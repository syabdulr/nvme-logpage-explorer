#!/usr/bin/env bash
# One-time host setup. Safe to re-run.
#
#   env/setup.sh            # install everything
#   env/setup.sh --check    # just report what's present
set -euo pipefail

ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PKGS=(qemu-system-x86 qemu-utils nvme-cli busybox-static)

check() {
  local ok=1
  for b in qemu-system-x86_64 qemu-img nvme busybox; do
    if command -v "$b" >/dev/null 2>&1; then
      printf '  ok    %-20s %s\n' "$b" "$(command -v "$b")"
    else
      printf '  MISS  %-20s\n' "$b"; ok=0
    fi
  done
  if command -v vng >/dev/null 2>&1; then
    printf '  ok    %-20s %s\n' "vng (virtme-ng)" "$(vng --version 2>/dev/null | head -1)"
  else
    printf '  MISS  %-20s (pipx install virtme-ng)\n' "vng"; ok=0
  fi
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then
    if qemu-system-x86_64 -device nvme,help 2>&1 | grep -qiE '^\s*ocp='; then
      printf '  ok    %-20s QEMU nvme device supports ocp=on\n' "OCP support"
    else
      printf '  warn  %-20s QEMU nvme device has no ocp property (0xC0 log unavailable)\n' "OCP support"
    fi
  fi
  [[ -e /dev/kvm ]] && printf '  ok    %-20s hardware virtualization available\n' "/dev/kvm" \
                    || printf '  info  %-20s absent — QEMU will use TCG emulation (slower boot, fine here)\n' "/dev/kvm"
  return $((1 - ok))
}

if [[ "${1:-}" == "--check" ]]; then
  echo "environment check:"; check; exit $?
fi

echo "installing: ${PKGS[*]}"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y "${PKGS[@]}"
else
  echo "non-apt system: install the equivalents of: ${PKGS[*]}" >&2
fi

if ! command -v vng >/dev/null 2>&1; then
  echo "installing virtme-ng via pipx"
  command -v pipx >/dev/null 2>&1 || sudo apt-get install -y pipx
  pipx install virtme-ng
fi

echo
echo "done. verify with: env/setup.sh --check"
