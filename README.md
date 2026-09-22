# NVMe Command & Log Page Explorer

Issue the core NVMe admin commands named in datacenter SSD test work — **Identify,
SMART/Health, Get Log Page, Dataset Management (Trim), and the OCP extended
health log** — against a live device, then parse and aggregate the telemetry the
way a monitoring or qualification pipeline would.

The device here is a QEMU-emulated NVMe controller with the OCP Datacenter SSD
feature set enabled, so the whole thing runs on a laptop with no special
hardware. Every command in [`docs/command-reference.md`](docs/command-reference.md)
is a real command issued to a real (emulated) controller through `nvme-cli`;
the findings in [`docs/findings.md`](docs/findings.md) are recorded from
actual output, not from the spec.

## What this exercises

| Area | Command(s) | Log Page ID |
|---|---|---|
| Controller / namespace identify | `nvme id-ctrl`, `nvme id-ns` | Identify (opcode 0x06), not a log page |
| SMART / Health | `nvme smart-log`, `nvme get-log --log-id 0x02` | 0x02 |
| Error Information | `nvme error-log`, `nvme get-log --log-id 0x01` | 0x01 |
| Firmware Slot Info | `nvme fw-log` | 0x03 |
| Commands Supported & Effects | `nvme effects-log` | 0x05 |
| OCP SMART / Health Extended | `nvme ocp smart-add-log` | 0xC0 |
| Trim / Deallocate | `nvme dsm --ad` | Dataset Management (opcode 0x09) |

The point of pulling SMART both ways — the `smart-log` convenience command and a
raw `get-log --log-id 0x02` — is to show the convenience commands are thin
wrappers over one generic Get Log Page mechanism parameterised by a log
identifier, offset, and length.

## The tool: `explore.py`

`explore.py` shells out to `nvme-cli` with `-o json`, normalises the output, and
builds tooling around it:

- **`snapshot`** — pull every log page above into one timestamped JSON document
- **`poll`** — sample SMART + OCP health on an interval into SQLite / CSV for
  trend analysis
- **`diff`** — structural diff of two snapshots (what moved after a workload)
- **`check`** — threshold rules over a snapshot: `available_spare` below its
  reported threshold, `percentage_used` at or above a limit, non-zero
  `critical_warning`, `media_errors` or `num_err_log_entries` incrementing
  between samples

This mirrors a real SSD-qualification loop: define an expected state, run a
workload, re-pull the logs, and flag what changed.

`check` is point-in-time — one snapshot against fixed thresholds. `poll`'s
output feeds a second, separate tool for the thing a single snapshot can't
show: a *trend* across many samples.

## Trend analysis: `analyze_telemetry.py`

```bash
pip install -r requirements-analysis.txt   # pandas + numpy; explore.py itself stays stdlib-only
python3 analyze_telemetry.py output/telemetry.csv
```

Loads a `poll --csv` output, fits a linear slope (`numpy.polyfit`) per tracked
column over elapsed hours, and flags anything moving the wrong way — spare
draining, `media_errors`/`num_err_log_entries` climbing, temperature
trending up — even when no single sample crosses a `check` threshold. Reuses
`explore.py`'s `Finding` type so both tools' output shapes match.

```
$ python3 analyze_telemetry.py samples/project1/telemetry_degrading_synthetic.csv
12 samples, 2026-09-10 19:00:00+00:00 -> 2026-09-11 06:00:00+00:00

                     first  last  delta  slope_per_hour   min    max   mean
column
available_spare      100.0  67.0  -33.0         -3.0000  67.0  100.0  83.50
media_errors            0.0   6.0    6.0          0.5629   0.0    6.0   1.75
...
[WARN] available_spare_trend: available_spare trending down at -3.0000/hr ...
[CRITICAL] media_errors_trend: media_errors trending up at +0.5629/hr ...
```

That CSV is synthetic (12 hourly rows, hand-authored to slope) — the real
committed capture, `samples/project1/telemetry.csv`, is 10 samples over 9
idle seconds and correctly reports **no adverse trends detected**; there's no
real degradation on an idle emulated drive to find. The synthetic file exists
to prove the trend/threshold logic actually fires, not to claim a finding
that didn't happen. `tests/test_analyze_telemetry.py` covers both cases.

## Running it

```bash
./env/setup.sh                       # one-time: qemu, nvme-cli, virtme-ng (uses sudo)
./env/up.sh -- scripts/capture.sh    # boot VM, run the full baseline→workload→diff→poll sequence
```

or step through it by hand:

```bash
./env/up.sh                                    # interactive shell in the VM, device at /dev/nvme0n1
  python3 explore.py snapshot -o output/baseline.json
  python3 explore.py check output/baseline.json
  dd if=/dev/zero of=/dev/nvme0n1 bs=1M count=32 oflag=direct
  nvme dsm /dev/nvme0n1 --slbs 0 --blocks 4096 --ad
  python3 explore.py snapshot -o output/after.json
  python3 explore.py diff output/baseline.json output/after.json
```

`make help` lists the wrapped targets. See [`env/README.md`](env/README.md) for
how the emulated device is built and the portable plain-QEMU fallback, and
[`samples/README.md`](samples/README.md) for the committed reference captures.

## Use as a library

Installable (stdlib only), and the capture / diff / rule logic is importable —
[`nvme-failure-injection-harness`](https://github.com/syabdulr/nvme-failure-injection-harness)
builds on it:

```bash
pip install git+https://github.com/syabdulr/nvme-logpage-explorer.git
```

```python
from explore import snapshot, compute_diff, evaluate, Thresholds

before = snapshot()                        # {meta, log_pages, smart_normalised}
# ... exercise the device ...
after = snapshot()
for c in compute_diff(before, after):
    print(c.path, c.before, "->", c.after, c.delta)
for f in evaluate(after, Thresholds(used_warn=80)):
    print(f.severity, f.rule, f.message)
```

## Results

Full write-up with real values in [`docs/findings.md`](docs/findings.md). Headlines
from `samples/project1/` (QEMU 10.2.1, nvme-cli 2.16, NVMe 1.4.0 device):

- **`smart-log` proven identical to `get-log --log-id 0x02`** — raw bytes decoded
  by hand (`00 43 01 …` → crit 0, temp 323 K, …) match the convenience command
  field-for-field.
- **OCP 0xC0 vs standard SMART** — 0xC0 (GUID `0xafd5…afc5`) adds physical media
  units, bad-NAND-block counts, ECC/XOR/E2E error counts, PLP capacitor health,
  PCIe error counts; standard 0x02 has none of these.
- **Workload diff** — a 32 MiB write shows up as exactly `33 554 432` in OCP
  *Physical Media Units Written* (bytes) and `+66` `data_units_written`
  (512 000-byte units); the raw log-page byte at offset 32 moves `0x02 → 0x46`.
- **Trim** — `nvme dsm --ad` succeeds but `nuse` is unchanged because the
  namespace doesn't advertise thin provisioning (`nsfeat` bit 0 clear) — a
  concrete example of a command completing while its *observable* effect depends
  on namespace configuration.

## Honest note on emulation

QEMU's NVMe model implements the command *interface* faithfully but many
wear/media counters (`media_errors`, `data_units_written`, most OCP 0xC0
fields) are static or zero — there is no real NAND underneath. Where a field
only moves on real silicon, `docs/findings.md` says so. The command paths, the
JSON parsing, the log-page semantics, and the tooling are all real and would
run unchanged against a physical drive.

## Layout

```
explore.py                 the tool
analyze_telemetry.py       pandas/numpy trend analysis over a poll CSV (optional extra)
tests/test_explore.py      unit tests for the decode / normalise / rule logic
tests/test_analyze_telemetry.py  trend-detection unit tests (skipped if pandas isn't installed)
env/                       emulated-device setup (vng + plain-QEMU fallback)
scripts/capture.sh         the full baseline→workload→diff→poll sequence
docs/command-reference.md  every command, annotated
docs/findings.md           recorded output + the OCP-vs-standard-SMART comparison
samples/project1/          committed JSON/CSV captures for reference
```

`make test` runs the unit tests (pure logic, no device); they also assert the
committed captures still decode consistently.
