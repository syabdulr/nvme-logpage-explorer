#!/usr/bin/env bash
# Fallback path: boot a full Ubuntu cloud-image guest under plain QEMU with the
# emulated OCP NVMe device attached. Slower to start than env/up.sh (a real
# distro boot, ~1-3 min under TCG) but fully self-contained and portable to any
# machine with QEMU.
#
#   env/launch-qemu.sh up       # download image, boot guest in background, wait for ssh
#   env/launch-qemu.sh ssh      # ssh into the running guest
#   env/launch-qemu.sh ssh nvme list
#   env/launch-qemu.sh down     # power off and clean runtime files
#
# The repo is exported to the guest at /mnt/repo (9p, read-only). Write captures
# to /mnt/repo is not possible; scp them back, or run explore.py with -o to
# /home/lab and `env/launch-qemu.sh pull <file>`.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

IMG_URL="${IMG_URL:-https://cloud-images.ubuntu.com/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-amd64.img}"
BASE_IMG="$ENV_DIR/$(basename "$IMG_URL")"
GUEST_IMG="$ENV_DIR/guest.qcow2"
SEED_ISO="$ENV_DIR/seed.iso"
SSH_KEY="$ENV_DIR/id_lab"
PIDFILE="$ENV_DIR/qemu.pid"
SSH_PORT="${SSH_PORT:-2222}"
SSH_BASE=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
          -i "$SSH_KEY")
SSH_OPTS=("${SSH_BASE[@]}" -p "$SSH_PORT")   # ssh: -p PORT
SCP_OPTS=("${SSH_BASE[@]}" -P "$SSH_PORT")   # scp: -P PORT

prepare() {
  require "$QEMU_BIN"; require qemu-img
  create_backing

  if [[ ! -f "$BASE_IMG" ]]; then
    log "downloading $(basename "$BASE_IMG") ..."
    curl -fL --progress-bar "$IMG_URL" -o "$BASE_IMG"
  fi
  if [[ ! -f "$GUEST_IMG" ]]; then
    log "creating overlay guest image (10G)"
    qemu-img create -f qcow2 -F qcow2 -b "$BASE_IMG" "$GUEST_IMG" 10G >/dev/null
  fi
  if [[ ! -f "$SSH_KEY" ]]; then
    log "generating throwaway ssh key"
    ssh-keygen -q -t ed25519 -N "" -f "$SSH_KEY" -C lab@nvme-lab
  fi
  if [[ ! -f "$SEED_ISO" ]]; then
    log "building cloud-init seed"
    local tmp; tmp="$(mktemp -d)"
    sed "s|SSH_PUBKEY_PLACEHOLDER|$(cat "$SSH_KEY.pub")|" \
        "$ENV_DIR/cloud-init/user-data" > "$tmp/user-data"
    cp "$ENV_DIR/cloud-init/meta-data" "$tmp/meta-data"
    if command -v cloud-localds >/dev/null 2>&1; then
      cloud-localds "$SEED_ISO" "$tmp/user-data" "$tmp/meta-data"
    elif command -v genisoimage >/dev/null 2>&1; then
      genisoimage -quiet -output "$SEED_ISO" -volid cidata -joliet -rock \
        "$tmp/user-data" "$tmp/meta-data"
    else
      die "need cloud-localds or genisoimage (apt install cloud-image-utils)"
    fi
    rm -rf "$tmp"
  fi
}

is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

up() {
  is_running && { log "already running (pid $(cat "$PIDFILE"))"; return; }
  prepare
  build_nvme_qemu_args
  local accel=tcg
  [[ -e /dev/kvm ]] && accel=kvm
  log "booting guest (accel=$accel) — ssh will come up on port $SSH_PORT"
  "$QEMU_BIN" \
    -name nvme-lab -machine q35,accel=$accel -cpu max -smp "${VM_CPUS:-4}" -m "${VM_MEM:-2G}" \
    -display none -vga none -serial file:"$ENV_DIR/console.log" -monitor none \
    -drive file="$GUEST_IMG",if=virtio,format=qcow2 \
    -drive file="$SEED_ISO",if=virtio,format=raw,readonly=on \
    "${NVME_QEMU_ARGS[@]}" \
    -netdev user,id=net0,hostfwd=tcp::"$SSH_PORT"-:22 -device virtio-net-pci,netdev=net0 \
    -virtfs local,path="$REPO_ROOT",mount_tag=repo,security_model=none,readonly=on \
    -pidfile "$PIDFILE" -daemonize
  log "waiting for ssh / cloud-init (first boot installs nvme-cli) ..."
  for i in $(seq 1 180); do
    if ssh "${SSH_OPTS[@]}" lab@127.0.0.1 'cloud-init status --wait >/dev/null 2>&1; command -v nvme' >/dev/null 2>&1; then
      log "guest ready. try: env/launch-qemu.sh ssh nvme list"
      return
    fi
    sleep 2
  done
  die "guest did not become ready — see $ENV_DIR/console.log"
}

ssh_guest() {
  is_running || die "guest not running (env/launch-qemu.sh up)"
  if [[ $# -gt 0 ]]; then
    ssh "${SSH_OPTS[@]}" lab@127.0.0.1 "$@"
  else
    ssh "${SSH_OPTS[@]}" lab@127.0.0.1
  fi
}

pull() {
  is_running || die "guest not running"
  local f="$1"
  mkdir -p "$REPO_ROOT/output"
  scp "${SCP_OPTS[@]}" lab@127.0.0.1:"$f" "$REPO_ROOT/output/"
  log "pulled $(basename "$f") -> output/"
}

down() {
  if is_running; then
    local pid; pid="$(cat "$PIDFILE")"
    log "powering off (pid $pid)"
    ssh "${SSH_OPTS[@]}" lab@127.0.0.1 'sudo poweroff' 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then log "still up, sending SIGTERM"; kill "$pid" 2>/dev/null || true; sleep 3; fi
    if kill -0 "$pid" 2>/dev/null; then log "forcing SIGKILL";        kill -9 "$pid" 2>/dev/null || true; sleep 1; fi
  fi
  rm -f "$PIDFILE"
  log "down. (guest.qcow2 / seed.iso / base image kept — 'clean' removes them)"
}

clean() {
  down
  rm -f "$GUEST_IMG" "$SEED_ISO" "$SSH_KEY" "$SSH_KEY.pub" "$ENV_DIR/console.log"
  log "removed runtime files (kept $(basename "$BASE_IMG"); delete it manually to re-download)"
}

case "${1:-up}" in
  up) up ;;
  ssh) shift; ssh_guest "$@" ;;
  pull) shift; pull "$@" ;;
  down) down ;;
  clean) clean ;;
  *) echo "usage: $0 {up|ssh [cmd...]|pull <guest-path>|down|clean}" >&2; exit 2 ;;
esac
