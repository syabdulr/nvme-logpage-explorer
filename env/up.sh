#!/usr/bin/env bash
# Bring up the emulated OCP NVMe device inside a fast virtme-ng VM that shares
# this repository.
#
#   env/up.sh                     # interactive shell in the guest, in the repo
#   env/up.sh -- nvme list        # run one command in the guest and exit
#   env/up.sh -- python3 explore.py snapshot -o output/baseline.json
#   env/up.sh -- scripts/capture.sh
#
# The guest kernel is the host kernel; output/ is bind-mounted read-write so
# anything written there lands back in the repo. Every other guest write is
# discarded on shutdown.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require vng
require "$QEMU_BIN"
create_backing
build_nvme_qemu_args
mkdir -p "$REPO_ROOT/output"

GUEST_CMD=()
if [[ "${1:-}" == "--" ]]; then shift; GUEST_CMD=("$@"); fi

VNG_ARGS=(
  --run
  --disable-kvm
  --cpus "${VM_CPUS:-4}"
  --memory "${VM_MEM:-2G}"
  --name nvme-lab
  --cwd "$REPO_ROOT"
  --rwdir "$REPO_ROOT/output"
  --quiet
)
# vng treats a leading-dash --qemu-opts value as a flag, so the passthrough goes
# as one bundled string (values have commas but no spaces — vng re-splits on ws).
VNG_ARGS+=(--qemu-opts="${NVME_QEMU_ARGS[*]}")

if [[ ${#GUEST_CMD[@]} -eq 0 ]]; then
  log "entering guest shell — namespace /dev/nvme0n1, controller /dev/nvme0"
  log "try:  nvme smart-log /dev/nvme0   |   python3 explore.py snapshot"
  exec vng "${VNG_ARGS[@]}"
fi

# Non-trivial guest commands (quotes, pipes, multiple args) survive far better
# through a generated script than through nested --exec quoting. The script lives
# in the r/w-shared output dir so the guest can read it.
RUNNER="$REPO_ROOT/output/.guest-exec.sh"
{
  # bash, not sh: printf %q below can emit $'...' ANSI-C quoting that dash rejects
  echo '#!/bin/bash'
  echo 'set -e'
  echo 'modprobe nvme 2>/dev/null || true'
  echo 'i=0; while [ ! -e /dev/nvme0n1 ] && [ $i -lt 50 ]; do i=$((i+1)); sleep 0.1; done'
  printf 'cd %q\n' "$REPO_ROOT"
  printf 'exec'; printf ' %q' "${GUEST_CMD[@]}"; echo
} > "$RUNNER"
chmod +x "$RUNNER"

log "guest exec: ${GUEST_CMD[*]}"
exec vng "${VNG_ARGS[@]}" --exec "$RUNNER"
