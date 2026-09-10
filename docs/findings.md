# Findings

Recorded from actual runs against the emulated OCP NVMe controller. The curated
captures referenced here are committed under `samples/project1/`; regenerate the
whole set with `env/up.sh -- scripts/capture.sh`.

## Environment

| | |
|---|---|
| QEMU | 10.2.1 (`-device nvme,...,ocp=on`) |
| nvme-cli | 2.16 |
| Kernel | 7.0.0-31-generic (host kernel, booted via virtme-ng) |
| Device | `QEMU NVMe Ctrl`, SN `OCP00000000DEADBEEF`, NVMe **1.4.0**, subnqn `nqn.2019-08.org.qemu:OCP00000000DEADBEEF` |
| Namespace | 2 GiB, `lbaf` 4 in use → **4096-byte** logical blocks, `nsze` 524288 blocks |

## 1. What Identify reports

From `samples/project1/baseline.json → log_pages.identify_controller`:

| Field | Value | Reading |
|---|---|---|
| `oncs` | 1885 (`0x75D`) | bit 2 set → **Dataset Management supported** (needed for the Trim step) |
| `lpa` | 7 | bits 0-2 → per-namespace SMART + Commands Supported & Effects log + extended Get Log Page all present |
| `mdts` | 7 | max transfer 2^7 pages |
| `elpe` | 0 | 0-based → 1 Error Information Log entry |
| `ver` | 66560 (`0x10400`) | NVMe 1.4.0 |
| `cctemp` / `wctemp` | 373 K / 343 K | critical / warning composite temp (100 °C / 70 °C) |

`identify_namespace`: `flbas = 4` selects LBA format 4 (`ds = 12` → 4096 B);
`nsze = ncap = nuse = 524288`. `nsfeat = 52` (`0x34`) — **bit 0 (thin
provisioning) is clear**, which matters for the Trim result below.

## 2. SMART two ways — `smart-log` vs generic Get Log Page (LID 0x02)

`explore.py` pulls the SMART/Health log both through the `smart-log` convenience
command and through the raw generic path
(`nvme get-log --log-id 0x02 --log-len 512 --raw-binary`), decodes the raw bytes
itself, and asserts they agree. From the baseline capture:

```
raw_first_48_hex: 00 43 01 00 00 00 00 00  00 00 00 00 00 00 00 00
                  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
                  02 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
```

| Byte offset | Field | Raw | Decoded | `smart-log` says |
|---|---|---|---|---|
| 0 | Critical Warning | `00` | 0 | 0 |
| 1–2 (LE) | Composite Temperature | `43 01` | 0x0143 = 323 K | 50 °C (323 K) |
| 3 / 4 | Available Spare / Threshold | `00` / `00` | 0 / 0 | 0 / 0 |
| 5 | Percentage Used | `00` | 0 | 0 |
| 32–47 (LE) | Data Units Read | `02 00…` | 2 | 2 |

`"matches_smart_log_command": true`. **`smart-log` is exactly
`get-log --log-id 0x02 --log-len 512`** — every convenience log command is a
Get Log Page with a fixed log identifier, offset, and length.

## 3. OCP 0xC0 (SMART / Health Extended) vs standard 0x02

`nvme ocp smart-add-log` returns the OCP Datacenter SSD log at `0xC0`, tagged
with **Log Page GUID `0xafd514c97c6f4f9ca4f2bfea2810afc5`** (the fixed OCP
identifier) and **Log Page Version 5**. What it adds over standard SMART:

| Category | Standard SMART 0x02 | OCP Extended 0xC0 (fields present on this device) |
|---|---|---|
| Endurance | `percentage_used` (one %) | Physical Media Units Written / Read (128-bit **bytes**), Endurance Estimate, Nand Avg Erase Count, Min/Max User Data Erase Counts |
| Media defects | `media_errors` (one counter) | Bad User NAND Blocks + Bad System NAND Blocks, each Raw **and** Normalized |
| Error correction | — | XOR Recovery Count, Uncorrectable Read Error Count, Soft ECC Error Count, End-to-End Detected / Corrected Errors |
| Thermal | `warning_temp_time`, `critical_comp_time` | Thermal Throttling Event Count + Current Throttling Status, Max Temperature Recorded |
| Power-loss protection | — | Capacitor Health, PLP Start Count |
| PCIe | — | PCIe Correctable Error Count, PCIe Link Retraining Count |
| Shutdown integrity | `unsafe_shutdowns` | Incomplete Shutdowns |
| Capacity | `nuse` (via Identify) | % Free Blocks, NUSE mirrored into the log, System Data % Used |
| Provenance | — | Log Page GUID + Version, DSSD firmware revision / build UUID / label, per-spec Errata Version fields |

The one OCP field that is **not** zero on QEMU is *Physical Media Units Read*
(`679936` bytes at baseline) — see §4 for it moving under load.

## 4. Workload effect — `explore.py diff baseline.json after.json`

Workload: `dd` 32 MiB (`oflag=direct`) → `nvme flush` → `dd` 32 MiB read-back.
11 fields changed; the counters that matter:

| Field | Before → After | Δ | Cross-check |
|---|---|---|---|
| OCP `Physical media units written.lo` | 0 → 33 554 432 | **+33 554 432** | exactly 32 MiB in **bytes** |
| OCP `Physical media units read.lo` | 679 936 → 35 581 952 | +34 902 016 | ~33.3 MiB (read-back + metadata) |
| `data_units_written` | 0 → 66 | +66 | ×512 000 B ≈ 33.8 MB |
| `data_units_read` | 2 → 70 | +68 | ×512 000 B ≈ 34.8 MB |
| `host_write_commands` | 0 → 96 | +96 | 32 MiB / 96 ≈ 341 KiB per command |
| `host_read_commands` | 45 → 228 | +183 | |

The generic-path decode tracks `smart_health` field-for-field, and byte 32 of the
raw log page moves `0x02 → 0x46` (2 → 70) — the counter is visible changing in
the wire bytes, not just in nvme-cli's rendering.

**Standard SMART reports host-visible units (512 000-byte "data units", command
counts). OCP 0xC0 reports the physical bytes the media actually saw.** On real
silicon the ratio of the two is write amplification; here they track 1:1 because
there is no FTL.

## 5. Trim / Dataset Management

```
nvme dsm /dev/nvme0n1 --slbs 0 --blocks 4096 --ad   →   NVMe DSM: success
```

`nuse` **did not change** (524288 → 524288). Reason: the namespace does not
advertise thin provisioning (`nsfeat` bit 0 clear), so `nuse` is pinned to
`ncap` regardless of deallocation. The DSM command itself completes with success
— it is a valid hint to the controller — but with this namespace configuration
there is no `nuse` accounting to observe it in. A namespace created with
`-device nvme,...,tp=on` (or real hardware) would show `nuse` drop.

## 6. What the tooling flags — `explore.py check`

- **Baseline:** one `INFO` — `available_spare and threshold both 0 → device does
  not report spare`. The naive rule "spare ≤ threshold ⇒ CRITICAL" would
  false-positive here (0 ≤ 0); `check` special-cases 0/0 as "not reported".
- **Strict run** (`--used-warn 0 --used-critical 1`): emits
  `[WARN] percentage_used: endurance used 0% ≥ 0%` — confirms the threshold
  engine trips and sets a non-zero exit code for CI gating.
- **`poll`** (`samples/project1/telemetry.csv`): 10 samples, 1 s apart, into
  SQLite + CSV. All counters flat on an idle drive — the columns are the ones a
  trend job would alert on (spare slope, `media_errors` / `num_err_log_entries`
  increments).

## 7. Other log pages

- **Error Information (0x01):** 1 entry, all zero — healthy idle drive. Project 2
  (failure injection) is what populates this.
- **Commands Supported & Effects (0x05):** 17 admin opcodes advertised; in the
  I/O set, DSM (9), Write (1), Write Zeroes (8), Flush (0), Copy (25) all carry
  the "Logical Block Content Change" effect bit (value `3` = supported +
  state-changing). A harness reads this before deciding what it may safely send.
- **Firmware Slot (0x03):** slot 1 active. nvme-cli renders the ASCII revision
  `"1.0"` as the integer `2314885530819505713` in JSON — a cosmetic nvme-cli
  quirk, the bytes are `31 2E 30 …` = `"1.0"`.

## Emulation limitations observed

| Field / behaviour | On QEMU | On a physical OCP drive |
|---|---|---|
| `available_spare` / threshold | fixed 0 / 0 | real spare-block accounting |
| `percentage_used`, endurance estimate, erase counts | static 0 | move with writes over the drive's life |
| `media_errors`, Bad NAND Blocks, ECC counters | static 0 | increment on real defects |
| `temperature` | fixed 323 K (50 °C) | live sensor, varies with load |
| `power_on_hours`, `power_cycles` | 0 | real |
| `nuse` after deallocate | unchanged (no thin-prov on this NS) | drops |
| write amplification (0xC0 physical vs host) | 1:1 (no FTL) | > 1 |

Every command path, status code, log-page layout, and JSON shape the tool
handles is real. What emulation cannot provide is the physics the counters
measure — so the tool is exercised here and would run unchanged against hardware.
