"""
Telemetry Agent — collects the three metric categories described in the paper:
  1. NIC counters      — bytes sent/received from /proc/net/dev
  2. Inter-node RTT    — TCP round-trip time to each peer node
  3. All-Reduce timing — wall-clock time of the gradient sync phase

Design decisions that match the paper's architecture:
  - RTT probes are gated to run ONLY during the idle window between epochs,
    detected by watching the epoch_sync file written by the training hook.
    This prevents probe traffic from competing with all-reduce bandwidth.
  - All metrics write to the shared SQLite database that the dashboard reads.
  - The agent runs as a separate process alongside training (not inside it),
    so it requires no changes to train.py. The all-reduce timing is collected
    via the timing hook injected by hook.py (imported in train.py).
  - No elevated privileges required — /proc/net/dev is world-readable,
    and RTT uses plain TCP sockets.

Usage (inside the telemetry container):
  python3 telemetry/agent.py --node-id node0 --job-id <job_id> --peers node1 node2
"""

import argparse
import os
import socket
import sqlite3
import time
import json
import threading
import psutil

# ── config ────────────────────────────────────────────────────────────────────
DB_PATH        = '/workspace/data/metrics.db'
SYNC_DIR       = '/workspace/data/sync'       # shared dir for epoch signals
NIC_INTERVAL   = 2.0    # seconds between NIC counter samples during training
RTT_PORT       = 19876  # port the RTT probe server listens on
RTT_TIMEOUT    = 2.0    # seconds before an RTT probe times out
RTT_PROBES     = 5      # number of pings per peer per measurement round
PROBE_PAYLOAD  = b'PING'


# ── database ──────────────────────────────────────────────────────────────────

def get_conn():
    """Return a SQLite connection. Called per-thread to avoid sharing."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")   # allows concurrent readers/writers
    return conn


def insert_metric(conn, node_id, job_id, epoch,
                  nic_sent, nic_recv, all_reduce_ms, rtt_ms):
    conn.execute("""
        INSERT INTO metrics
            (timestamp, node_id, job_id, nic_bytes_sent, nic_bytes_recv,
             all_reduce_ms, rtt_ms, epoch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (time.time(), node_id, job_id,
          nic_sent, nic_recv, all_reduce_ms, rtt_ms, epoch))
    conn.commit()


# ── NIC counters ──────────────────────────────────────────────────────────────

def read_nic_counters(iface=None):
    """
    Read cumulative bytes sent/received from /proc/net/dev.
    Returns (bytes_sent, bytes_recv) as a snapshot (not a rate).
    If iface is None, sums across all non-loopback interfaces.
    """
    stats = psutil.net_io_counters(pernic=True)
    total_sent = 0
    total_recv = 0
    for name, counters in stats.items():
        if name == 'lo':
            continue
        if iface and name != iface:
            continue
        total_sent += counters.bytes_sent
        total_recv += counters.bytes_recv
    return total_sent, total_recv


def compute_nic_rate(snap1, snap2, elapsed):
    """
    Convert two cumulative snapshots into bytes/sec rates.
    snap = (bytes_sent, bytes_recv)
    """
    sent_rate = (snap2[0] - snap1[0]) / elapsed if elapsed > 0 else 0
    recv_rate = (snap2[1] - snap1[1]) / elapsed if elapsed > 0 else 0
    return int(sent_rate), int(recv_rate)


# ── RTT probe server ──────────────────────────────────────────────────────────

def start_rtt_server(stop_event):
    """
    Lightweight TCP echo server. Runs in a background thread.
    Peers connect, send PING, we echo it back immediately.
    This is what makes RTT measurement possible without ICMP privileges.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', RTT_PORT))
    server.listen(10)
    server.settimeout(1.0)

    print(f"[RTT server] Listening on port {RTT_PORT}")

    while not stop_event.is_set():
        try:
            conn, _ = server.accept()
            data = conn.recv(64)
            conn.sendall(data)   # echo immediately — minimizes server processing time
            conn.close()
        except socket.timeout:
            continue
        except Exception as e:
            if not stop_event.is_set():
                print(f"[RTT server] Error: {e}")

    server.close()


def measure_rtt(peer_host, num_probes=RTT_PROBES):
    """
    Measure round-trip time to a peer by opening a TCP connection,
    sending a small payload, and timing the echo response.
    Returns median RTT in milliseconds, or None if unreachable.
    
    Using TCP (not ICMP ping) so no special privileges are needed,
    consistent with the paper's no-elevated-privileges requirement.
    """
    rtts = []
    for _ in range(num_probes):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(RTT_TIMEOUT)
            t0 = time.perf_counter()
            sock.connect((peer_host, RTT_PORT))
            sock.sendall(PROBE_PAYLOAD)
            sock.recv(64)
            t1 = time.perf_counter()
            sock.close()
            rtts.append((t1 - t0) * 1000)   # convert to ms
        except Exception:
            pass

    if not rtts:
        return None

    rtts.sort()
    return rtts[len(rtts) // 2]   # median — more robust than mean


def measure_all_rtts(peers):
    """
    Measure RTT to every peer and return the worst (max) RTT.
    We use max because all-reduce performance is bounded by the slowest
    communication path — matching the T_comm = max(T_comm_node_i) model
    from the paper's insights section.
    """
    worst_rtt = None
    results = {}
    for peer in peers:
        rtt = measure_rtt(peer)
        if rtt is not None:
            results[peer] = rtt
            print(f"  [RTT] → {peer}: {rtt:.2f} ms")
        else:
            print(f"  [RTT] → {peer}: unreachable")

    if results:
        worst_rtt = max(results.values())
    return worst_rtt, results


# ── epoch sync ────────────────────────────────────────────────────────────────
# The training hook (hook.py) writes a small JSON file at the end of each
# epoch containing the all-reduce timing for that epoch. The agent watches
# this file to know:
#   (a) when an epoch has completed (safe to run RTT probes)
#   (b) what the all-reduce time was for that epoch

def wait_for_epoch_signal(job_id, node_id, last_epoch, timeout=300):
    """
    Block until the training hook signals that a new epoch has completed.
    Reads the per-node signal file so each agent gets its own node's
    actual measured all-reduce time, not rank 0's shared value.
    Returns (epoch_number, all_reduce_ms) or None on timeout.
    """
    signal_path = os.path.join(SYNC_DIR, f'{job_id}_{node_id}_epoch.json')
    deadline = time.time() + timeout

    while time.time() < deadline:
        if os.path.exists(signal_path):
            try:
                with open(signal_path, 'r') as f:
                    data = json.load(f)
                epoch = data.get('epoch', 0)
                if epoch > last_epoch:
                    return epoch, data.get('all_reduce_ms', None)
            except (json.JSONDecodeError, KeyError):
                pass
        time.sleep(0.2)

    return None


def read_final_signal(job_id, node_id):
    """Check if this node's training has signalled completion."""
    done_path = os.path.join(SYNC_DIR, f'{job_id}_{node_id}_done')
    return os.path.exists(done_path)


# ── main agent loop ───────────────────────────────────────────────────────────

def run_agent(node_id, job_id, peers):
    """
    Main collection loop. Runs for the duration of one training job.

    Per-epoch flow:
      1. Collect NIC rate (sampled continuously in background thread)
      2. Wait for epoch-complete signal from training hook
      3. In the idle window after the epoch: run RTT probes to all peers
      4. Read all-reduce timing from the epoch signal
      5. Write one metric row to the database
    """
    os.makedirs(SYNC_DIR, exist_ok=True)

    conn = get_conn()
    stop_event = threading.Event()

    # Start RTT echo server so peers can probe us
    server_thread = threading.Thread(
        target=start_rtt_server,
        args=(stop_event,),
        daemon=True
    )
    server_thread.start()

    # NIC sampling state — we track running averages between epoch signals
    nic_snap_prev = read_nic_counters()
    nic_time_prev = time.time()
    nic_sent_rate = 0
    nic_recv_rate = 0

    # Background NIC sampling thread updates rates continuously
    nic_lock = threading.Lock()

    def nic_sampler():
        nonlocal nic_snap_prev, nic_time_prev, nic_sent_rate, nic_recv_rate
        while not stop_event.is_set():
            time.sleep(NIC_INTERVAL)
            snap = read_nic_counters()
            now  = time.time()
            with nic_lock:
                elapsed = now - nic_time_prev
                s, r = compute_nic_rate(nic_snap_prev, snap, elapsed)
                nic_sent_rate = s
                nic_recv_rate = r
                nic_snap_prev = snap
                nic_time_prev = now

    nic_thread = threading.Thread(target=nic_sampler, daemon=True)
    nic_thread.start()

    print(f"[Agent] Started | node={node_id} | job={job_id} | peers={peers}")

    last_epoch = 0

    while True:
        # Wait for training to signal epoch complete
        result = wait_for_epoch_signal(job_id, node_id, last_epoch)

        if result is None:
            print("[Agent] Timeout waiting for epoch signal — assuming training finished")
            break

        epoch, all_reduce_ms = result
        last_epoch = epoch
        print(f"[Agent] Epoch {epoch} complete | all_reduce={all_reduce_ms:.1f}ms")

        # RTT probes run NOW — in the idle window between epochs
        # This is the gating mechanism described in the challenges section
        if peers:
            print(f"[Agent] Running RTT probes (idle window)...")
            worst_rtt, _ = measure_all_rtts(peers)
        else:
            worst_rtt = None

        # Snapshot current NIC rates
        with nic_lock:
            sent = nic_sent_rate
            recv = nic_recv_rate

        insert_metric(
            conn,
            node_id=node_id,
            job_id=job_id,
            epoch=epoch,
            nic_sent=sent,
            nic_recv=recv,
            all_reduce_ms=all_reduce_ms,
            rtt_ms=worst_rtt
        )

        print(f"[Agent] Wrote metrics | nic_sent={sent/1e6:.1f}MB/s "
              f"nic_recv={recv/1e6:.1f}MB/s rtt={worst_rtt}ms")

        # Check if training is done
        if read_final_signal(job_id, node_id):
            print(f"[Agent] Training complete signal received. Shutting down.")
            break

    stop_event.set()
    conn.close()
    print("[Agent] Exiting.")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Telemetry agent for distributed training')
    parser.add_argument('--node-id', required=True,
                        help='This node\'s identifier, e.g. node0')
    parser.add_argument('--job-id',  required=True,
                        help='Job identifier shared across all nodes for this run')
    parser.add_argument('--peers',   nargs='*', default=[],
                        help='Hostnames of peer nodes to probe, e.g. node1 node2')
    args = parser.parse_args()

    run_agent(args.node_id, args.job_id, args.peers)