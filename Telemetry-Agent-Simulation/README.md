# ECE590 Telemetry Agent — Setup & Run Guide

Distributed training telemetry system for quantifying node-level degradation
in DDP training jobs. Collects NIC counters, inter-node RTT, and all-reduce
timing from each node during training, scores node health, and displays results
in a live dashboard.

---

## Prerequisites

- **Docker Desktop** installed and running
- **Python 3.11+** with a virtual environment (for downloading MNIST locally)
- All commands are run from the project root: `ECE590Project/`

---

## Project Structure

```
ECE590Project/
├── dashboard/
│   ├── app.py              # Flask dashboard (http://localhost:5050)
│   ├── health_scores.py    # Computes node health scores from metrics
│   ├── fake_metrics.py     # Simulates telemetry data (dev only)
│   └── init_db.py          # Creates the SQLite database schema
├── training/
│   └── train.py            # DDP training script with telemetry hook
├── telemetry/
│   ├── __init__.py
│   ├── agent.py            # Main telemetry collection loop
│   ├── hook.py             # Injected into train.py to measure all-reduce timing
│   └── launch.py           # Starts agent + training together on one node
├── data/                   # Shared volume — database and MNIST live here
├── results/                # Training logs written here
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## First-Time Setup

### Step 1 — Download MNIST locally

The containers cannot access the internet to download MNIST, so download it
once on your host machine. Activate your Python venv first, then run:

```bash
python -c "from torchvision.datasets import MNIST; MNIST('./data', download=True)"
```

This creates `data/MNIST/` in your project root. The containers mount this
folder so training can find the dataset without any network access.

### Step 2 — Create the Docker network (run once ever)

```bash
docker network create training-net
```

### Step 3 — Build and start all containers

```bash
docker-compose up --build -d
```

This builds the image and starts 5 containers: node0, node1, node2, dashboard,
and telemetry. The `-d` flag runs them in the background.

Verify all containers are running:
```bash
docker ps
```

You should see all 5 containers with status `Up`.

### Step 4 — Initialize the database (run once ever)

```bash
docker exec -it dashboard python3 dashboard/init_db.py
```

---

## Running a Training Job

You need **3 terminals open at the same time** — one per node. All three
must be started within a few seconds of each other or node0 will time out
waiting for the others to connect.

Pick a job ID (e.g. `job001`) and use the same one across all three terminals.

**Terminal 1:**
```bash
docker exec -it node0 python3 telemetry/launch.py --node-id node0 --peers node1 node2 --job-id job001
```

**Terminal 2:**
```bash
docker exec -it node1 python3 telemetry/launch.py --node-id node1 --peers node0 node2 --job-id job001
```

**Terminal 3:**
```bash
docker exec -it node2 python3 telemetry/launch.py --node-id node2 --peers node0 node1 --job-id job001
```

When the cluster connects you will see:
```
[Master] Cluster ready — 3 nodes connected | job_id=job001
```

Training runs for 3 epochs. When all three terminals show `[Launch] Done`,
training is complete.

> **Note:** Use a different job ID for each run (job001, job002, etc.).
> Reusing the same ID will mix metrics from different runs in the database.

---

## Viewing Results

### Compute health scores

After training finishes, run this to score each node based on collected metrics:

```bash
docker exec -it dashboard python3 dashboard/health_scores.py
```

You will see each node's score printed with a bar chart, ranked best to worst.

### Open the dashboard

The dashboard starts automatically when the containers start. Just open:

```
http://localhost:5050
```

It auto-refreshes every 10 seconds. After running `health_scores.py` the score
cards will update. The per-epoch metrics table shows all-reduce timing, RTT,
and NIC throughput per node per epoch with status tags (healthy / slow / timeout).

---

## Running Multiple Jobs

Each job adds data to the database and the health scores are updated using
exponential moving average (EMA) — so nodes that perform consistently well
across many jobs will score higher than nodes that have occasional issues.

To run another job, just increment the job ID:
```bash
# Same commands as above but --job-id job002
```

Then recompute health scores to see the updated historical ranking:
```bash
docker exec -it dashboard python3 dashboard/health_scores.py
```

---

## Resetting the Database

To clear all metrics and start fresh:

```bash
docker exec -it dashboard python3 -c "
import sqlite3
conn = sqlite3.connect('/workspace/data/metrics.db')
conn.execute('DELETE FROM metrics')
conn.execute('DELETE FROM health_scores')
conn.commit()
conn.close()
print('Database cleared')
"
```

Also clear the sync files so old epoch signals don't interfere:
```bash
docker exec -it node0 rm -f /workspace/data/sync/*
```

---

## Troubleshooting

**`EADDRINUSE` on port 29500**
A previous training run didn't exit cleanly and the port is still held.
Restart the containers:
```bash
docker-compose restart
```

**Node0 hangs at `[Launch] Starting training...` for more than 30 seconds**
The other nodes haven't connected yet. Open Terminal 2 and Terminal 3 and
start node1 and node2. They must all be running before training can proceed.

**`No such file or directory` for a telemetry file**
The containers were built before the telemetry folder was added. Rebuild:
```bash
docker-compose up --build -d
```

**Dashboard shows `Internal Server Error`**
Check the logs:
```bash
docker logs dashboard
```

**Health scores look wrong after a run**
Make sure you're using a unique job ID for each run. If you reused a job ID,
clear the database (see above) and rerun with a fresh ID.

**Containers won't start / port conflicts**
Bring everything down and back up:
```bash
docker-compose down
docker-compose up -d
```

---

## How It Works

```
train.py  ──(hook.py)──►  /workspace/data/sync/<job_id>_<node_id>_epoch.json
                                          │
agent.py  ◄──── watches per-node file ───┘
    │
    ├── NIC counters   →  bytes/sec from psutil (/proc/net/dev)
    ├── RTT probes     →  TCP echo to peers (idle window between epochs only)
    └── All-reduce ms  →  wall-clock time of gradient sync phase
                                          │
                                          ▼
                              /workspace/data/metrics.db
                                          │
                              health_scores.py (EMA scoring)
                                          │
                                    dashboard (Flask)
                                  http://localhost:5050
```

The key design: RTT probes only run during the idle window between epochs,
gated by the epoch signal file written by `hook.py`. This prevents probe
traffic from competing with all-reduce bandwidth during training.

---

## Moving to Unity Cluster

The simulation validates the core system. To run on Unity:

1. Replace `docker-compose` node launching with SLURM job steps
2. Replace the shared SQLite volume with a shared NFS filesystem or network database
3. Replace the sync file IPC with a socket server or NFS-mounted sync directory
4. The agent, hook, health scoring, and dashboard code are otherwise unchanged
