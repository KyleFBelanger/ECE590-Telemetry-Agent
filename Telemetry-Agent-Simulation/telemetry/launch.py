"""
launch.py — starts the telemetry agent and training together on one node.

Run this inside each node container instead of calling train.py directly.
It starts the agent as a background process, then runs training, then
ensures the agent exits cleanly when training is done.

Usage (inside a node container):
  python3 telemetry/launch.py --node-id node0 --peers node1 node2 node3 node4

Environment variables expected (set by docker-compose.yml):
  MASTER_ADDR, MASTER_PORT, WORLD_SIZE, RANK

Optional:
  JOB_ID — if not set, train.py generates one and broadcasts it.
"""

import argparse
import os
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--node-id', required=True,
                        help='This node\'s name, e.g. node0')
    parser.add_argument('--peers',   nargs='*', default=[],
                        help='Peer hostnames to probe, e.g. node1 node2 node3 node4')
    parser.add_argument('--job-id',  default=None,
                        help='Optional job ID — auto-generated if not provided')
    args = parser.parse_args()

    env = os.environ.copy()
    if args.job_id:
        env['JOB_ID'] = args.job_id

    # Build agent command
    agent_cmd = [
        sys.executable, 'telemetry/agent.py',
        '--node-id', args.node_id,
        '--job-id',  args.job_id or 'auto',   # agent will read from sync file if 'auto'
        '--peers',   *args.peers
    ]

    # Start the agent first so the RTT server is up before training begins
    print(f"[Launch] Starting telemetry agent on {args.node_id}...")
    agent_proc = subprocess.Popen(agent_cmd, env=env)
    time.sleep(1.0)   # give the RTT server a moment to bind
    if agent_proc.poll() is not None:
        print(
            f"[Launch] Telemetry agent exited early on {args.node_id} "
            f"with code {agent_proc.returncode}."
        )
        return agent_proc.returncode or 1

    # Start training
    print(f"[Launch] Starting training on {args.node_id}...")
    train_cmd  = [sys.executable, 'training/train.py']
    train_proc = subprocess.Popen(train_cmd, env=env)

    # Wait for training to finish
    train_returncode = train_proc.wait()
    print(f"[Launch] Training finished on {args.node_id}.")

    # Agent will exit on its own via the done signal, but give it a moment
    try:
        agent_proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        print("[Launch] Agent did not exit cleanly — terminating.")
        agent_proc.terminate()
        try:
            agent_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("[Launch] Agent still running — killing.")
            agent_proc.kill()
            agent_proc.wait()

    print(f"[Launch] Done.")
    return train_returncode or agent_proc.returncode or 0


if __name__ == '__main__':
    raise SystemExit(main())
