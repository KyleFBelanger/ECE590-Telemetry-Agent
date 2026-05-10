# ECE590 Telemetry Agent

Distributed training telemetry simulation for quantifying node-level degradation
in DDP training jobs. The system collects NIC counters, inter-node RTT, and
all-reduce timing from five training containers, stores metrics in SQLite, and
shows live results in a Flask dashboard.

## Project Structure

```text
dashboard/
  app.py              Flask dashboard and /api/dashboard polling endpoint
  auto_score.py       Background health score updater
  health_scores.py    Health score formula and manual scorer entrypoint
  init_db.py          SQLite schema initializer
scripts/
  check_db.sh
  clean_stale_processes.sh
  reset_experiment.sh
  run_job.sh
  show_job_averages.sh
  show_rtt_matrix.sh
  show_scores.sh
  apply_netem.sh
  clear_netem.sh
  show_netem.sh
training/
  train.py            DDP training script with telemetry hook
telemetry/
  agent.py            Telemetry collection loop
  hook.py             Training hook for all-reduce timing and epoch signals
  launch.py           Starts the telemetry agent and training together
data/                 Shared database, sync files, and MNIST data
results/              Per-job node logs
```

Assumptions:
- Run commands from the project root.
- Keep `data/MNIST/` in place; reset scripts do not delete it.
- If `/workspace/data/metrics.db` does not exist yet, initialize it with
  `docker exec -it dashboard python3 dashboard/init_db.py`.

## Boot The Project

Create the shared Docker network and start all services:

```bash
docker network create training-net 2>/dev/null || true
docker compose up -d --build
```

Expected containers:

```text
node0
node1
node2
node3
node4
dashboard
telemetry
scorer
```

Open the dashboard:

```text
http://127.0.0.1:5050
```

The dashboard is Flask and polls `/api/dashboard` every 2 seconds. The `scorer`
container runs `dashboard/auto_score.py` and automatically recomputes
`health_scores`, so you no longer need to manually run `health_scores.py` after
each job.

## Reproducible Workflow

Reset metrics, health scores, and sync files:

```bash
./scripts/reset_experiment.sh
```

To also clear old per-node job logs:

```bash
./scripts/reset_experiment.sh --logs
```

Run a baseline job:

```bash
./scripts/run_job.sh baseline001
```

Logs are written to:

```text
results/<job_id>_node*.log
```

The default training cluster is now five nodes: `node0`, `node1`, `node2`,
`node3`, and `node4`. `WORLD_SIZE=5`, and `run_job.sh` launches all five nodes
automatically. To run a temporary 3-node job for debugging, override the node
list:

```bash
NODES="node0 node1 node2" ./scripts/run_job.sh three_node_debug_001
```

Show raw per-node averages for a job:

```bash
./scripts/show_job_averages.sh baseline001
```

A correct default 3-epoch 5-node job should produce 3 metric rows for each
node, for 15 total rows in `metrics`. Use `show_job_averages.sh` to verify that
node0 through node4 all show `rows` equal to `3`.

Run a longer job by setting `EPOCHS`:

```bash
EPOCHS=10 ./scripts/run_job.sh five_big_baseline_001
```

## 5-Node Validation Workflow

Boot:

```bash
docker network create training-net 2>/dev/null || true
docker compose up -d --build
```

Reset:

```bash
./scripts/reset_experiment.sh
```

Five-node baseline:

```bash
./scripts/run_job.sh five_baseline_001
./scripts/show_job_averages.sh five_baseline_001
./scripts/show_rtt_matrix.sh five_baseline_001
./scripts/check_db.sh
```

Five-node degraded node test:

```bash
./scripts/apply_netem.sh node3 delay 20ms
./scripts/run_job.sh five_node3_delay_20ms_001
./scripts/clear_netem.sh node3
./scripts/show_job_averages.sh five_node3_delay_20ms_001
./scripts/show_rtt_matrix.sh five_node3_delay_20ms_001
./scripts/check_db.sh
```

Expected:
- baseline: low RTT paths across all nodes, and the recommendation avoids nobody
- degraded node3: high RTT paths involving node3, healthy paths among the other nodes, and the recommendation avoids node3

Longer job:

```bash
EPOCHS=10 ./scripts/run_job.sh five_big_baseline_001
```

## Netem Experiments

The Docker image installs `iproute2`, and node containers have `NET_ADMIN` so
`tc/netem` can degrade a selected node interface.

Apply delay to a node:

```bash
./scripts/apply_netem.sh node3 delay 20ms
./scripts/show_netem.sh node3
./scripts/run_job.sh five_node3_delay_20ms_001
./scripts/clear_netem.sh node3
```

Apply packet loss:

```bash
./scripts/apply_netem.sh node3 loss 5%
```

Apply bandwidth limiting:

```bash
./scripts/apply_netem.sh node3 rate 10mbit
```

Combine settings:

```bash
./scripts/apply_netem.sh node3 delay 50ms loss 2% rate 10mbit
```

Always clear netem after a degradation run unless you intentionally want the
next run to inherit the same network condition:

```bash
./scripts/clear_netem.sh node3
```

## Recovery and Safe Testing Workflow

Clean stale training and telemetry processes without stopping containers:

```bash
./scripts/clean_stale_processes.sh
```

Check SQLite database integrity:

```bash
./scripts/check_db.sh
```

Expected healthy output:

```text
integrity_check: ok
```

Reset experiment history safely:

```bash
./scripts/reset_experiment.sh
```

`reset_experiment.sh` checks database integrity first. If SQLite reports a
malformed database, the script backs up `metrics.db`, `metrics.db-wal`, and
`metrics.db-shm` into `data/bad_db_backups/<timestamp>/`, recreates the schema,
then clears metrics, health scores, and sync files.

After pressing Ctrl+C during a job, run:

```bash
./scripts/clean_stale_processes.sh
./scripts/clear_netem.sh node3
./scripts/check_db.sh
```

Do not reuse the same job ID after an interrupted run unless you intentionally
delete the old partial rows with `reset_experiment.sh`. Prefer a new job ID,
for example `five_node3_delay_20ms_002` instead of rerunning
`five_node3_delay_20ms_001`.

`run_job.sh` now runs stale-process cleanup before every job and traps Ctrl+C
so it can kill leftover `telemetry/launch.py`, `telemetry/agent.py`, and
`training/train.py` processes inside node containers.

## Recommended Experiment Sequence

```bash
./scripts/reset_experiment.sh --logs

./scripts/run_job.sh five_baseline_001

./scripts/apply_netem.sh node3 delay 20ms
./scripts/run_job.sh five_node3_delay_20ms_001
./scripts/clear_netem.sh node3

./scripts/apply_netem.sh node3 loss 5%
./scripts/run_job.sh five_node3_loss_5pct_001
./scripts/clear_netem.sh node3

./scripts/apply_netem.sh node3 rate 10mbit
./scripts/run_job.sh five_node3_rate_10mbit_001
./scripts/clear_netem.sh node3

EPOCHS=10 ./scripts/run_job.sh five_big_baseline_001

./scripts/apply_netem.sh node3 delay 20ms
EPOCHS=10 ./scripts/run_job.sh five_big_node3_delay_20ms_001
./scripts/clear_netem.sh node3
```

After any run, inspect raw averages:

```bash
./scripts/show_job_averages.sh five_baseline_001
```

## Manual Scoring

Manual scoring remains available for debugging:

```bash
docker exec -it dashboard python3 dashboard/health_scores.py
```

Normal experiment runs should rely on the `scorer` container instead.

To inspect the current score table and recent job averages:

```bash
./scripts/show_scores.sh
```

## Health Scoring

Health scores are intended to identify meaningful node or link degradation,
not tiny baseline noise. The original pure min-max approach was too harsh:
when all nodes were healthy and separated by only fractions of a millisecond,
one node could still be forced toward `0.000` simply because it was the worst
of a nearly identical group.

The scorer now applies deadbands before min-max normalization:

```text
all_reduce spread under 2 ms       -> nodes are effectively tied
RTT spread under 1 ms              -> nodes are effectively tied
NIC total spread under 10% median  -> nodes are effectively tied
```

If a metric is inside its deadband, every node gets full credit for that metric.
This prevents a healthy baseline from creating fake stragglers.

Interpretation notes:
- Lower all-reduce time is better, but DDP all-reduce is collective. A degraded
  node or link can slow the whole job, so all nodes may show high all-reduce.
- Lower RTT is better and helps identify communication-path issues.
- Higher useful NIC throughput is generally better during training, but raw NIC
  bytes are not a perfect congestion detector because unrelated background
  traffic can inflate counters.
- The recommendation layer should be built after scoring behavior is stable.

Manual scoring prints per-job diagnostics, including node scores, raw averages,
component scores, and whether each metric was inside or outside deadband:

```bash
docker exec -it dashboard python3 dashboard/health_scores.py
```

## Scoring Validation Flow

Baseline validation:

```bash
./scripts/reset_experiment.sh
./scripts/run_job.sh five_baseline_001
./scripts/show_job_averages.sh five_baseline_001
./scripts/check_db.sh
```

20ms delay validation:

```bash
./scripts/apply_netem.sh node3 delay 20ms
./scripts/run_job.sh five_node3_delay_20ms_001
./scripts/clear_netem.sh node3
./scripts/show_job_averages.sh five_node3_delay_20ms_001
./scripts/check_db.sh
```

50ms delay validation:

```bash
./scripts/apply_netem.sh node3 delay 50ms
./scripts/run_job.sh five_node3_delay_50ms_001
./scripts/clear_netem.sh node3
./scripts/show_job_averages.sh five_node3_delay_50ms_001
./scripts/check_db.sh
```

Expected pattern:
- Baseline: all nodes should have high health, and no healthy node should score
  near zero because of tiny differences.
- 20ms delay: raw averages should show cluster slowdown.
- 50ms delay: raw averages should show stronger cluster slowdown.
- Every default 3-epoch run should show 3 metric rows per node.
- `./scripts/check_db.sh` should return `integrity_check: ok`.

## Per-Peer RTT And Recommendations

The `metrics` table stores one worst RTT value per node per epoch. That is
useful for the dashboard summary, but it hides which peer path was slow.

The `rtt_metrics` table stores directed per-peer RTT rows:

```text
node_id -> peer_node_id, job_id, epoch, rtt_ms
```

For a default 5-node, 3-epoch job, expect:

```text
metrics rows:     15  (5 nodes x 3 epochs)
rtt_metrics rows: 60  (5 nodes x 4 peers x 3 epochs)
```

Inspect per-peer RTT paths with:

```bash
./scripts/show_rtt_matrix.sh <job_id>
```

The dashboard includes an advisory "Scheduler Recommendation" section. It does
not launch jobs or enforce placement. It reads `health_scores` plus recent
per-peer RTT path data and suggests which nodes look safer to prefer or avoid
for future jobs.

The recommendation is intentionally simple and explainable:
- prefer high health scores
- prefer low recent RTT involving the node
- avoid repeated high-RTT or timeout paths
- say "No clearly degraded node detected" when risks are similar

## Advisory Scheduler Recommendation Layer

The advisory scheduler recommendation layer reads `health_scores` and
per-peer RTT data from `rtt_metrics`. It identifies nodes that appear on
high-latency or timeout paths, then recommends which nodes to prefer or avoid
for future distributed training jobs.

This is advisory only. It does not replace SLURM, Kubernetes, or any real
cluster scheduler, and the dashboard never launches jobs automatically.

Inspect the recommendation directly:

```bash
./scripts/show_recommendation.sh
./scripts/show_recommendation.sh <job_id>
```

Recommended validation workflow:

```bash
./scripts/reset_experiment.sh
./scripts/run_job.sh five_baseline_clean_001
./scripts/show_job_averages.sh five_baseline_clean_001
./scripts/show_rtt_matrix.sh five_baseline_clean_001
./scripts/show_recommendation.sh five_baseline_clean_001
```

Expected: avoid nobody.

Then degrade node1:

```bash
./scripts/apply_netem.sh node1 delay 20ms
./scripts/run_job.sh five_node1_delay_20ms_001
./scripts/show_rtt_matrix.sh five_node1_delay_20ms_001
./scripts/show_recommendation.sh five_node1_delay_20ms_001
```

Expected: avoid node1 because it appears on high-latency RTT paths.

Scheduler-style demo validation:

```bash
NODES="node0 node2 node3 node4" ./scripts/run_job.sh recommended_without_node1_001
./scripts/clear_netem.sh node1
./scripts/show_job_averages.sh recommended_without_node1_001
./scripts/show_rtt_matrix.sh recommended_without_node1_001
```

There is also a convenience helper that reads the current recommendation and
runs the next job with the recommended nodes using the existing `NODES`
override:

```bash
./scripts/run_recommended_job.sh recommended_without_node1_001 five_node1_delay_20ms_001
```

Suggested RTT/recommendation workflow:

```bash
./scripts/reset_experiment.sh
./scripts/run_job.sh five_baseline_001
./scripts/show_job_averages.sh five_baseline_001
./scripts/show_rtt_matrix.sh five_baseline_001

./scripts/apply_netem.sh node3 delay 20ms
./scripts/run_job.sh five_node3_delay_20ms_001
./scripts/clear_netem.sh node3
./scripts/show_job_averages.sh five_node3_delay_20ms_001
./scripts/show_rtt_matrix.sh five_node3_delay_20ms_001
```

Expected result:
- baseline jobs show low RTT paths between all nodes
- degraded node/link experiments show high RTT paths involving the degraded node
- the dashboard recommendation suggests avoiding the degraded node when
  confidence is high enough

For a 5-node job, `show_rtt_matrix.sh` should show 20 directed node pairs.
When node3 is degraded, high RTT paths should involve node3 while paths among
node0, node1, node2, and node4 remain comparatively low.

## Troubleshooting

If containers are not running:

```bash
docker ps
docker compose logs dashboard
docker compose logs scorer
```

If the dashboard says the database is missing:

```bash
docker exec -it dashboard python3 dashboard/init_db.py
```

If a training run hangs or port `29500` is busy:

```bash
docker compose restart node0 node1 node2 node3 node4
```

If a degraded network condition seems to persist:

```bash
./scripts/clear_netem.sh node0
./scripts/clear_netem.sh node1
./scripts/clear_netem.sh node2
./scripts/clear_netem.sh node3
./scripts/clear_netem.sh node4
```

## How It Works

```text
train.py  -- hook.py --> /workspace/data/sync/<job_id>_<node_id>_epoch_<epoch>.json
                                      |
agent.py  <----- watches sync files --+
    |
    +-- NIC counters
    +-- RTT probes
    +-- All-reduce ms
    |
    v
/workspace/data/metrics.db
    |
    +-- auto_score.py updates health_scores
    |
    v
Flask dashboard at http://127.0.0.1:5050
```

RTT probes run during the idle window between epochs, gated by epoch signal
files from `hook.py`, so probe traffic does not intentionally compete with the
all-reduce phase.
