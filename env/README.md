# Emulated NVMe device

Both launch paths attach the **same** emulated controller: a QEMU `nvme` device
with the OCP Datacenter SSD feature set enabled (`ocp=on`), one namespace backed
by a raw file (`env/nvme-backing.raw`, git-ignored), 4 KiB logical blocks, and a
32 KiB discard granularity so Dataset Management / Trim has a visible effect.

Device parameters live in `common.sh` and are overridable by environment
variable (`NVME_SIZE`, `NVME_SERIAL`, `NVME_LBADS`, ...).

## Path A — `env/up.sh` (default, fast)

Uses [virtme-ng](https://github.com/arighi/virtme-ng): boots the **host kernel**
in QEMU against a throwaway overlay of the live filesystem. No disk image to
download, boots in seconds even without KVM, and this repo is already present in
the guest at its real path. `output/` is bind-mounted read-write so captures
persist; every other guest write is discarded at shutdown.

```
env/up.sh                    # interactive shell, cwd = repo, device = /dev/nvme0n1
env/up.sh -- nvme list       # one command, then exit
env/up.sh -- python3 explore.py snapshot -o output/baseline.json
```

The `nvme` host driver is autoloaded from the PCI modalias during coldplug; the
script also `modprobe nvme` as a fallback.

## Path B — `env/launch-qemu.sh` (fallback, portable)

Boots a full Ubuntu 24.04 minimal cloud image under plain QEMU. Slower first
boot (cloud-init installs `nvme-cli` + `python3`), but self-contained and
identical to how you'd stand this up on a machine without virtme-ng. SSH on
`localhost:2222`, repo exported read-only at `/mnt/repo` over 9p. Needs
`cloud-image-utils` (`cloud-localds`) for the seed ISO — `env/setup.sh` installs
it.

Verified working on this machine: guest boots (its own 6.8 kernel), the emulated
`nvme0` enumerates, `nvme ocp smart-add-log` returns the 0xC0 log, and
`explore.py` runs against it. Note the guest ships nvme-cli **2.8** vs the host's
**2.16** on Path A — a useful reminder that log-page *bytes* are stable across
tool versions even when the rendering changes. The findings in `docs/` were
captured via Path A.

```
env/launch-qemu.sh up                 # download image + boot + wait for ssh
env/launch-qemu.sh ssh                 # shell in the guest
env/launch-qemu.sh ssh nvme smart-log /dev/nvme0
env/launch-qemu.sh pull baseline.json  # copy a file from guest ~ to ./output/
env/launch-qemu.sh down                # power off
env/launch-qemu.sh clean               # + delete overlay image / seed / key
```

## Why emulation is enough here

QEMU implements the NVMe **command interface** to spec — admin/IO opcodes, the
Get Log Page mechanism, log page layouts, DSM, the OCP 0xC0 log — so every
command path, status code, and JSON shape the tooling handles is real. What it
does **not** model is NAND: wear, media errors, and most OCP endurance counters
are static because there is no flash underneath. `docs/findings.md` marks every
field that only moves on physical hardware.
