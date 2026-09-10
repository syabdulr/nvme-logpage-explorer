#!/usr/bin/env bash
# Bring up the emulated OCP NVMe device inside a fast virtme-ng VM that shares
# this repository.
#
#   env/up.sh                     # interactive shell in the guest, in the repo
#   env/up.sh -- nvme list        # run one command in the guest and exit
#   env/up.sh -- python3 explore.py snapshot -o output/baseline.json
#
# The guest kernel is the host kernel; output/ is bind-mounted read-write so
# captures written there land back in the repo. Everything else the guest
# writes is discarded on shutdown.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require vng
require "$QEMU_BIN"
create_backing
build_nvme_qemu_args
mkdir -p "$REPO_ROOT/output"

# Command to run in the guest: everything after `--`, or an interactive shell.
GUEST_CMD=()
if [[ "${1:-}" == "--" ]]; then
  shift
  GUEST_CMD=("$@")
fi

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
for a in "${NVME_QEMU_ARGS[@]}"; do VNG_ARGS+=(--qemu-opts "$a"); done

# udev autoloads nvme.ko from the PCI modalias; modprobe is a belt-and-braces
# fallback in case coldplug raced the device.
PRELUDE='(modprobe nvme 2>/dev/null || true); for i in $(seq 1 50); do [ -e /dev/nvme0n1 ] && break; sleep 0.1; done'

if [[ ${#GUEST_CMD[@]} -gt 0 ]]; then
  printf -v joined '%q ' "${GUEST_CMD[@]}"
  log "guest exec: ${GUEST_CMD[*]}"
  exec vng "${VNG_ARGS[@]}" --exec "sudo sh -c '$PRELUDE'; $joined"
else
  log "entering guest shell — namespace /dev/nvme0n1, controller /dev/nvme0"
  log "try:  sudo nvme smart-log /dev/nvme0   |   python3 explore.py snapshot"
  exec vng "${VNG_ARGS[@]}"
fi
