#!/usr/bin/env bash
# Full capture sequence for docs/findings.md. Run inside the emulated-device VM:
#
#   env/up.sh -- scripts/capture.sh
#
# Everything lands in output/ (shared back to the host). Curate the keepers into
# samples/project1/ from the host afterwards.
set -euo pipefail

CTRL=/dev/nvme0
NS=/dev/nvme0n1
OUT=output
mkdir -p "$OUT"

hr() { printf '\n=== %s ===\n' "$*"; }

hr "baseline snapshot"
python3 explore.py snapshot -o "$OUT/baseline.json"

hr "threshold check on baseline"
python3 explore.py check "$OUT/baseline.json" || true

hr "namespace utilisation BEFORE workload"
nvme id-ns "$NS" -o json | python3 -c 'import json,sys;d=json.load(sys.stdin);print({k:d[k] for k in ("nsze","ncap","nuse")})'

hr "workload: 32 MiB write, flush, read-back"
dd if=/dev/zero of="$NS" bs=1M count=32 oflag=direct conv=fdatasync status=none
nvme flush "$NS"
dd if="$NS" of=/dev/null bs=1M count=32 iflag=direct status=none

hr "Trim: deallocate the first 4096 logical blocks (DSM --ad)"
nvme dsm "$NS" --slbs 0 --blocks 4096 --ad

hr "namespace utilisation AFTER trim"
nvme id-ns "$NS" -o json | python3 -c 'import json,sys;d=json.load(sys.stdin);print({k:d[k] for k in ("nsze","ncap","nuse")})'

hr "post-workload snapshot"
python3 explore.py snapshot -o "$OUT/after.json"

hr "diff baseline -> after"
python3 explore.py diff "$OUT/baseline.json" "$OUT/after.json" || true

hr "10-sample SMART poll (1s interval)"
python3 explore.py poll --interval 1 --count 10 \
  --database "$OUT/telemetry.sqlite" --csv "$OUT/telemetry.csv"

hr "check with an intentionally strict endurance limit (demonstrates a WARN/CRIT)"
python3 explore.py check "$OUT/baseline.json" --used-warn 0 --used-critical 1 || true

hr "done — artifacts in $OUT/"
ls -la "$OUT/"
