# Telemetry Project Setup

This project sets up a distributed training cluster with telemetry monitoring and a dashboard.

## Architecture

- **Training Nodes**: 3 worker nodes (node0, node1, node2) running PyTorch distributed training.
- **Dashboard**: Web interface to view health scores and metrics.
- **Telemetry Agent**: Collects metrics from training nodes and writes to shared SQLite database.

## Shared Database Schema

The telemetry agent writes to `metrics` table, dashboard reads and computes health scores in `health_scores` table.

See `dashboard/init_db.py` for the exact schema.

## Setup

1. **Create external network** (run once):
   ```bash
   docker network create training-net
   ```

2. **Build and start containers**:
   ```bash
   docker-compose up --build
   ```

3. **Initialize database** (run in dashboard container):
   ```bash
   docker exec -it dashboard python3 dashboard/init_db.py
   ```

4. **Start training** (in each node container):
   ```bash
   docker exec -it node0 python3 training/train.py
   docker exec -it node1 python3 training/train.py
   docker exec -it node2 python3 training/train.py
   ```

5. **Start telemetry agent** (in telemetry container, once your code is ready):
   ```bash
   docker exec -it telemetry python3 telemetry/your_agent.py
   ```

## Development

- **Fake metrics**: Use `dashboard/fake_metrics.py` to simulate telemetry data.
- **Health scores**: Run `dashboard/health_scores.py` to compute scores from metrics.

## Files to Share

- `Dockerfile`
- `docker-compose.yml`
- `dashboard/init_db.py` (database schema)
- This README
