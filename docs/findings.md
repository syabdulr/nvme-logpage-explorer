# Findings

Recorded from actual runs against the emulated OCP NVMe controller. Regenerate
the raw captures with `env/up.sh -- python3 explore.py snapshot -o output/<name>.json`;
the curated ones referenced here are committed under `samples/project1/`.

> Status: **in progress** — capture pipeline built, populating results.

## Environment

| | |
|---|---|
| QEMU | _TBD_ |
| nvme-cli | _TBD_ |
| Device | `QEMU NVMe OCP Ctrl`, serial `OCP00000000DEADBEEF`, 2 GiB, 4 KiB LBA |
| Launch path | `env/up.sh` (virtme-ng, host kernel _TBD_) |

## 1. What Identify reports

_TBD from `output/baseline.json` → `log_pages.identify_controller` / `identify_namespace`._

- `oncs` DSM bit set? (expected: yes — needed for the Trim step)
- `lpa` bits: per-namespace SMART? effects log?
- active `lbaf` / block size:

## 2. SMART two ways — `smart-log` vs generic `get-log 0x02`

Show the convenience command and the raw Get Log Page return the same bytes.

_TBD: paste both, note that `smart-log` = `get-log --log-id 0x02 --log-len 512`._

## 3. OCP 0xC0 vs standard 0x02 SMART

| Category | Standard SMART (0x02) | OCP Extended (0xC0) |
|---|---|---|
| Endurance | `percent_used` (single %) | + Physical Media Units Written/Read (128-bit), Endurance Estimate, per-type Erase Counts |
| Media defects | `media_errors` (one lumped counter) | + Bad User NAND Blocks, Bad System NAND Blocks (raw + normalized) |
| Error correction | — | + XOR Recovery, Uncorrectable Read, Soft ECC, End-to-End detected/corrected |
| Thermal | `warning_temp_time`, `critical_comp_time` | + Thermal Throttling Status, Throttling Event Count |
| Power-loss protection | — | + Capacitor Health, PLP Start Count |
| PCIe | — | + PCIe Correctable Error Count |
| Shutdown integrity | `unsafe_shutdowns` | + Incomplete Shutdowns, Incapacitated Shutdowns |

_TBD: replace with the actual field list and values from the capture; note which
are non-zero on QEMU vs which need real NAND._

## 4. Trim / Dataset Management effect

Baseline → `nvme dsm --slbs 0 --blocks 128 --ad` → re-snapshot → `explore.py diff`.

_TBD: does `id-ns` `nuse` change? do any SMART fields move? (On QEMU, expect
`nuse` to drop and wear counters to stay flat.)_

## 5. What the tooling flags

`explore.py check` output on baseline and on a deliberately-tripped threshold
(`--used-warn 0`), plus a `poll` run showing trend columns.

_TBD._

## Emulation limitations observed

_TBD: concrete list of fields that stayed static, cross-referenced against what a
physical OCP drive would report._
