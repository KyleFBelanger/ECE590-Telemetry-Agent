"""
Health score computation.

Implements the design we drafted in the paper:
  H(node, job) = w1 * R_ar + w2 * R_tp + w3 * R_lat
where R_x is min-max normalized relative performance.

Then aggregates per-job scores into historical scores using EMA:
  H_hist(node) = alpha * H(job) + (1-alpha) * H_hist_prev(node)

Run this after every job completes to update the historical scores.
"""
import sqlite3
import time

DB_PATH = '/workspace/data/metrics.db'

# Weights for the three signals (must sum to 1.0)
W_ALL_REDUCE = 0.5  # most direct signal of straggler behavior
W_THROUGHPUT = 0.3  # leading indicator
W_LATENCY    = 0.2  # noisier, less weight

# EMA smoothing — how much a new job affects the historical score
ALPHA = 0.3  # 30% weight on new observation, 70% on history

EPSILON = 1e-9  # prevents division by zero


def normalize(value, min_v, max_v, lower_is_better=True):
    """Min-max normalize a value to [0, 1]. Higher = healthier."""
    if max_v - min_v < EPSILON:
        return 1.0  # all nodes equal — everyone gets perfect score
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

    # Find min/max across nodes for normalization
    ar_vals  = [r[1] for r in rows]
    tp_vals  = [r[2] for r in rows]
    rtt_vals = [r[3] for r in rows]

    ar_min,  ar_max  = min(ar_vals),  max(ar_vals)
    tp_min,  tp_max  = min(tp_vals),  max(tp_vals)
    rtt_min, rtt_max = min(rtt_vals), max(rtt_vals)

    scores = {}
    for node_id, ar, tp, rtt in rows:
        # All-reduce: lower is better
        r_ar  = normalize(ar,  ar_min,  ar_max,  lower_is_better=True)
        # Throughput: higher is better
        r_tp  = normalize(tp,  tp_min,  tp_max,  lower_is_better=False)
        # RTT: lower is better
        r_lat = normalize(rtt, rtt_min, rtt_max, lower_is_better=True)

        score = W_ALL_REDUCE * r_ar + W_THROUGHPUT * r_tp + W_LATENCY * r_lat
        scores[node_id] = score

    return scores


def update_historical_scores(conn, job_scores):
    """Apply EMA to update each node's long-term score."""
    now = time.time()

    for node_id, new_score in job_scores.items():
        # Look up current historical score
        row = conn.execute(
            "SELECT current_score, total_jobs FROM health_scores WHERE node_id = ?",
            (node_id,)
        ).fetchone()

        if row is None:
            # First job for this node — score is just the job score
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
    """Process every job in the metrics table that hasn't been scored yet.
    For simplicity right now we just recompute everything."""
    conn = sqlite3.connect(DB_PATH)

    # Clear existing historical scores (for simplicity — production would track which jobs already scored)
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

    # Show final historical scores
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
