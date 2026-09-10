# Shared configuration for the emulated NVMe device.
# Sourced by up.sh / launch-qemu.sh — not executed directly.

set -euo pipefail

ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ENV_DIR/.." && pwd)"

# Backing store for the emulated namespace. Kept out of git (see .gitignore).
NVME_BACKING="${NVME_BACKING:-$ENV_DIR/nvme-backing.raw}"
NVME_SIZE="${NVME_SIZE:-2G}"
NVME_SERIAL="${NVME_SERIAL:-OCP00000000DEADBEEF}"
NVME_MODEL="${NVME_MODEL:-QEMU NVMe OCP Ctrl}"

# 4 KiB logical / 4 KiB physical, 32 KiB discard granularity so DSM/Trim is visible.
NVME_LBADS="${NVME_LBADS:-4096}"
NVME_DISCARD_GRAN="${NVME_DISCARD_GRAN:-32768}"

QEMU_BIN="${QEMU_BIN:-qemu-system-x86_64}"

log()  { printf '\033[1;34m[env]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[env]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[env]\033[0m %s\n' "$*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 || die "missing '$1' — run env/setup.sh first"
}

create_backing() {
  if [[ -f "$NVME_BACKING" ]]; then
    log "backing file exists: $NVME_BACKING ($(du -h "$NVME_BACKING" | cut -f1))"
    return
  fi
  require qemu-img
  log "creating $NVME_SIZE backing file: $NVME_BACKING"
  qemu-img create -f raw "$NVME_BACKING" "$NVME_SIZE" >/dev/null
}

# Does this QEMU build expose the OCP property on the nvme device?
qemu_has_ocp() {
  "$QEMU_BIN" -device nvme,help 2>&1 | grep -qiE '^\s*ocp='
}

# Populate the global array NVME_QEMU_ARGS with the QEMU flags that attach the
# emulated OCP NVMe controller + namespace. Values contain commas but no spaces,
# so a plain word array is safe.
NVME_QEMU_ARGS=()
build_nvme_qemu_args() {
  local ocp=""
  if qemu_has_ocp; then
    ocp=",ocp=on"
    log "QEMU nvme device: ocp=on"
  else
    warn "this QEMU build has no 'ocp' nvme property — 0xC0 log will be unavailable"
  fi
  NVME_QEMU_ARGS=(
    -drive "file=$NVME_BACKING,if=none,id=nvm0,format=raw,discard=on,detect-zeroes=unmap"
    -device "nvme,drive=nvm0,serial=$NVME_SERIAL,id=nvme0,logical_block_size=$NVME_LBADS,physical_block_size=$NVME_LBADS,discard_granularity=$NVME_DISCARD_GRAN$ocp"
  )
}
