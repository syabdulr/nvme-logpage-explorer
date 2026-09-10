#!/usr/bin/env bash
# A small, deliberately boring workload to move the host-side SMART counters
# (host_write_commands, data_units_written, host_read_commands) and then
# deallocate part of it, so `explore.py diff` / `poll` have something to show.
#
# Runs inside the emulated-device VM:  env/up.sh -- scripts/workload.sh
set -euo pipefail

NS="${1:-/dev/nvme0n1}"

echo "[workload] target: $NS"
sudo nvme id-ns "$NS" -o json | grep -E '"(nsze|nuse|ncap)"' || true

echo "[workload] writing 64 MiB of zeros to the head of the namespace"
sudo dd if=/dev/zero of="$NS" bs=1M count=64 oflag=direct conv=fdatasync status=none

echo "[workload] reading it back"
sudo dd if="$NS" of=/dev/null bs=1M count=64 iflag=direct status=none

echo "[workload] flushing the controller cache"
sudo nvme flush "$NS" || true

echo "[workload] deallocating the first 128 blocks (Trim / DSM --ad)"
sudo nvme dsm "$NS" --slbs 0 --blocks 128 --ad

echo "[workload] post-workload namespace utilisation"
sudo nvme id-ns "$NS" -o json | grep -E '"(nsze|nuse|ncap)"' || true
echo "[workload] done"
