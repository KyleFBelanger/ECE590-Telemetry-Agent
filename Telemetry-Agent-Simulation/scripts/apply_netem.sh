#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <node0|node1|node2> <delay|loss|rate> <value> [more netem args...]" >&2
  exit 2
fi

node="$1"
shift

case "$node" in
  node0|node1|node2) ;;
  *)
    echo "First argument must be node0, node1, or node2." >&2
    exit 2
    ;;
esac

docker exec -i "$node" sh -s -- "$@" <<'SH'
set -eu

iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
if [ -z "${iface:-}" ]; then
  iface="eth0"
fi

tc qdisc del dev "$iface" root 2>/dev/null || true
tc qdisc add dev "$iface" root netem "$@"

echo "Applied netem on $iface:"
tc qdisc show dev "$iface"
SH
