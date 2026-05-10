#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <job_id>" >&2
  exit 2
fi

job_id="$1"
mkdir -p results

pids=()
nodes=()
statuses=()
cleaned=0

cleanup_training() {
  if [[ "$cleaned" -eq 1 ]]; then
    return
  fi
  cleaned=1

  for pid in "${pids[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done

  ./scripts/clean_stale_processes.sh || true
}

on_interrupt() {
  echo
  echo "Interrupted. Cleaning up training and telemetry processes..."
  cleanup_training
  exit 130
}

trap on_interrupt INT TERM

run_node() {
  local node="$1"
  shift
  local log_path="results/${job_id}_${node}.log"

  if [[ -n "${EPOCHS:-}" ]]; then
    docker exec -e EPOCHS="$EPOCHS" "$node" python3 telemetry/launch.py \
      --node-id "$node" --peers "$@" --job-id "$job_id" >"$log_path" 2>&1
  else
    docker exec "$node" python3 telemetry/launch.py \
      --node-id "$node" --peers "$@" --job-id "$job_id" >"$log_path" 2>&1
  fi
}

echo "Cleaning stale processes before starting ${job_id}..."
./scripts/clean_stale_processes.sh

docker exec node0 sh -lc 'mkdir -p /workspace/data/sync && rm -f /workspace/data/sync/*'

echo "Starting job ${job_id} across node0, node1, and node2..."
if [[ -n "${EPOCHS:-}" ]]; then
  echo "Using EPOCHS=${EPOCHS}"
fi

run_node node0 node1 node2 &
pids+=("$!")
nodes+=("node0")
statuses+=("-1")

run_node node1 node0 node2 &
pids+=("$!")
nodes+=("node1")
statuses+=("-1")

run_node node2 node0 node1 &
pids+=("$!")
nodes+=("node2")
statuses+=("-1")

remaining=${#pids[@]}
status=0

while [[ "$remaining" -gt 0 ]]; do
  for i in "${!pids[@]}"; do
    if [[ "${statuses[$i]}" != "-1" ]]; then
      continue
    fi

    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      if wait "${pids[$i]}"; then
        rc=0
      else
        rc=$?
      fi
      statuses[$i]="$rc"
      remaining=$((remaining - 1))
      echo "${nodes[$i]} exited with code ${rc}"

      if [[ "$rc" -ne 0 ]]; then
        status=1
        echo "${nodes[$i]} failed. Cleaning up remaining training and telemetry processes..." >&2
        cleanup_training
      fi
    fi
  done
  sleep 1
done

trap - INT TERM

echo "Logs written to:"
echo "  results/${job_id}_node0.log"
echo "  results/${job_id}_node1.log"
echo "  results/${job_id}_node2.log"
echo "Dashboard: http://127.0.0.1:5050"

if [[ "$status" -ne 0 ]]; then
  echo "Job ${job_id} failed on at least one node. Check the logs above." >&2
  exit "$status"
fi

echo "Job ${job_id} complete. The scorer service will update health_scores automatically."
