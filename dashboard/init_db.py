"""
Initializes the SQLite database with our two tables.
Run this once before starting the system.
Safe to run multiple times — uses CREATE TABLE IF NOT EXISTS.
"""
import sqlite3
import os

DB_PATH = '/workspace/data/metrics.db'

def init_database():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # Raw time-series metrics from telemetry agent
    cur.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       REAL    NOT NULL,
            node_id         TEXT    NOT NULL,
            job_id          TEXT    NOT NULL,
            nic_bytes_sent  INTEGER,
            nic_bytes_recv  INTEGER,
            all_reduce_ms   REAL,
            rtt_ms          REAL,
            epoch           INTEGER
        )
    """)

    # Indexes to make dashboard queries fast
    cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_node ON metrics(node_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_job  ON metrics(job_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_time ON metrics(timestamp)")

    # Computed health scores (one row per node, updated over time)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS health_scores (
            node_id        TEXT    PRIMARY KEY,
            current_score  REAL    NOT NULL,
            last_updated   REAL    NOT NULL,
            total_jobs     INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

if __name__ == "__main__":
    init_database()
