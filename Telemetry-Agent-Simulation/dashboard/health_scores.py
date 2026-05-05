"""
Health score computation.

Implements the design we drafted in the paper:
  H(node, job) = w1 * R_ar + w2 * R_tp + w3 * R_lat
where R_x is min-max normalized relative performance.

Then aggregates per-job scores into historical scores using EMA:
  H_hist(node) = alpha * H(job) + (1-alpha) * H_hist_prev(node)

Fixes vs original:
  1. None RTT values are treated as a penalty (worst score) rather than
     crashing or being silently ignored — a missing probe means the node
     was unreachable, which is bad, not neutral.
  2. all_reduce_ms is averaged per-node rather than being identical across
     nodes. Since rank 0 writes the same value for all nodes in the current
     setup, we fall back to RTT and throughput as the differentiating signals
     and reduce all_reduce weight when variance is zero.
"""
import sqlite3
import time

DB_PATH = '/workspace/data/metrics.db'

# Weights for the three signals (must sum to 1.0)
W_ALL_REDUCE = 0.5
W_THROUGHPUT = 0.3
W_LATENCY    = 0.2

ALPHA   = 0.3    # EMA — 30% new job, 70% history
EPSILON = 1e-9


def normalize(value, min_v, max_v, lower_is_better=True):
    """Min-max normalize to [0,1]. Higher output = healthier node."""
    if max_v - min_v < EPSILON:
        return 1.0  # no variance — all nodes equal on this signal
    score = (value - min_v) / (max_v - min_v)
    return 1.0 - score if lower_is_better else score


def compute_job_score(conn, job_id):
    """Compute health score for each node for a single job."""
    rows = conn.execute("""
        SELECT node_id,
               AVG(all_reduce_ms),
               AVG(nic_bytes_sent + nic_bytes_recv),
               AVG(rtt_ms)
        FROM metrics
        WHERE job_id = ?
        GROUP BY node_id
    """, (job_id,)).fetchall()

    if not rows:
        return {}

    # ── handle None RTT values ────────────────────────────────────────────────
    # A None RTT means the probe timed out — the node was unreachable during
    # the idle window. We treat this as worse than any measured value by
    # substituting a penalty value: max observed RTT * 2.
    # This ensures unreachable nodes score lower, not the same as healthy ones.
    measured_rtts = [r[3] for r in rows if r[3] is not None]
    rtt_penalty   = (max(measured_rtts) * 2.0) if measured_rtts else 100.0

    cleaned = []
    for node_id, ar, tp, rtt in rows:
        rtt_clean = rtt if rtt is not None else rtt_penalty
        cleaned.append((node_id, ar, tp, rtt_clean))

    # ── find min/max for normalization ────────────────────────────────────────
    ar_vals  = [r[1] for r in cleaned]
    tp_vals  = [r[2] for r in cleaned]
    rtt_vals = [r[3] for r in cleaned]

    ar_min,  ar_max  = min(ar_vals),  max(ar_vals)
    tp_min,  tp_max  = min(tp_vals),  max(tp_vals)
    rtt_min, rtt_max = min(rtt_vals), max(rtt_vals)

    # ── if all_reduce has no variance, redistribute its weight to RTT ─────────
    # This happens when rank 0 broadcasts a single value to all nodes.
    # In that case the signal is useless for differentiation, so we give
    # its weight to latency which is the next most direct straggler signal.
    if ar_max - ar_min < EPSILON:
        w_ar  = 0.0
        w_tp  = W_THROUGHPUT
        w_lat = W_ALL_REDUCE + W_LATENCY   # RTT absorbs the unused weight
    else:
        w_ar  = W_ALL_REDUCE
        w_tp  = W_THROUGHPUT
        w_lat = W_LATENCY

    scores = {}
    for node_id, ar, tp, rtt in cleaned:
        r_ar  = normalize(ar,  ar_min,  ar_max,  lower_is_better=True)
        r_tp  = normalize(tp,  tp_min,  tp_max,  lower_is_better=False)
        r_lat = normalize(rtt, rtt_min, rtt_max, lower_is_better=True)

        score = w_ar * r_ar + w_tp * r_tp + w_lat * r_lat
        scores[node_id] = round(score, 3)

    return scores


def update_historical_scores(conn, job_scores):
    """Apply EMA to update each node's long-term score."""
    now = time.time()
    for node_id, new_score in job_scores.items():
        row = conn.execute(
            "SELECT current_score, total_jobs FROM health_scores WHERE node_id = ?",
            (node_id,)
        ).fetchone()

        if row is None:
            conn.execute("""
                INSERT INTO health_scores (node_id, current_score, last_updated, total_jobs)
                VALUES (?, ?, ?, 1)
            """, (node_id, new_score, now))
        else:
            old_score, total_jobs = row
            updated = ALPHA * new_score + (1 - ALPHA) * old_score
            conn.execute("""
                UPDATE health_scores
                SET current_score = ?, last_updated = ?, total_jobs = ?
                WHERE node_id = ?
            """, (updated, now, total_jobs + 1, node_id))

    conn.commit()


def process_all_jobs():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM health_scores")
    conn.commit()

    job_ids = [r[0] for r in conn.execute(
        "SELECT job_id FROM metrics GROUP BY job_id ORDER BY MIN(timestamp)"
    ).fetchall()]

    print(f"Processing {len(job_ids)} jobs...\n")
    for job_id in job_ids:
        scores = compute_job_score(conn, job_id)
        update_historical_scores(conn, scores)
        print(f"Job {job_id}:")
        for node, s in sorted(scores.items()):
            print(f"  {node}: {s:.3f}")
        print()

    print("=" * 50)
    print("FINAL HISTORICAL HEALTH SCORES")
    print("=" * 50)
    rows = conn.execute("""
        SELECT node_id, current_score, total_jobs
        FROM health_scores
        ORDER BY current_score DESC
    """).fetchall()

    for node_id, score, jobs in rows:
        bar = '█' * int(score * 30)
        print(f"  {node_id}: {score:.3f}  {bar}  ({jobs} jobs)")

    conn.close()


if __name__ == "__main__":
    process_all_jobs()