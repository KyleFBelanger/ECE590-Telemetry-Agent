#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <node0|node1|node2>" >&2
  exit 2
fi

node="$1"
case "$node" in
  node0|node1|node2) ;;
  *)
    echo "Argument must be node0, node1, or node2." >&2
    exit 2
    ;;
esac

docker exec -i "$node" sh -s <<'SH'
set -eu

iface="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
if [ -z "${iface:-}" ]; then
  iface="eth0"
fi

echo "Current qdisc on $iface:"
tc qdisc show dev "$iface"
SH
