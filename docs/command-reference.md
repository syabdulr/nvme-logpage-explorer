# Command reference

Every command the project issues, what it maps to at the protocol level, and the
fields worth reading in the output. Device nodes below: `/dev/nvme0` is the
**controller** (admin commands), `/dev/nvme0n1` is the **namespace** (I/O and
namespace-scoped commands).

## Identify — `nvme id-ctrl` / `nvme id-ns`

```
nvme id-ctrl /dev/nvme0   -o json
nvme id-ns   /dev/nvme0n1 -o json
```

Identify is admin opcode `0x06`; a CNS (Controller or Namespace Structure) value
selects which 4096-byte structure comes back. This is the "what am I talking to"
step.

- **id-ctrl**: `mn` (model), `sn` (serial), `fr` (firmware rev), `oncs`
  (optional NVM commands — bit 2 = Dataset Management supported), `lpa` (log page
  attributes — bit 0 = per-namespace SMART, bit 1 = Commands Supported & Effects
  log), `elpe` (error log page entries), `mdts` (max data transfer size).
- **id-ns**: `nsze` / `ncap` / `nuse` (size / capacity / in-use, in logical
  blocks), `lbaf` array + `flbas` (formatted LBA size — the active `lbaf` gives
  the block size, e.g. 2^12 = 4096), `nsfeat` bit 0 = thin provisioning /
  deallocate reporting.

## SMART / Health — Log Page `0x02`

Two ways to the same log page:

```
nvme smart-log /dev/nvme0 -o json                       # convenience command
nvme get-log   /dev/nvme0 --log-id 0x02 --log-len 512   # the generic mechanism
```

`smart-log` *is* a Get Log Page for LID `0x02` with a 512-byte transfer. The
generic `get-log` takes a log identifier, an offset (`--lpo`), and a length
(`--log-len`) and returns raw bytes — every "convenience" log command is one of
these underneath. Key fields:

| Field | Meaning |
|---|---|
| `critical_warning` | bitfield: 0 spare low · 1 temp · 2 reliability degraded · 3 read-only · 4 volatile-mem backup failed · 5 PMR |
| `temperature` | composite temperature in **Kelvin** (subtract 273 for °C) |
| `avail_spare` / `spare_thresh` | remaining spare blocks (%) and the threshold the drive warns at |
| `percent_used` | vendor estimate of endurance consumed (%), can exceed 100 |
| `data_units_read` / `data_units_written` | count of 512-byte units × 1000 |
| `host_read_commands` / `host_write_commands` | cumulative command counts |
| `media_errors` | unrecovered data integrity errors (uncorrectable ECC, CRC, etc.) |
| `num_err_log_entries` | lifetime count of entries ever placed in the Error Information log |
| `unsafe_shutdowns`, `power_cycles`, `power_on_hours` | |

## Error Information — Log Page `0x01`

```
nvme error-log /dev/nvme0 -o json
```

Returns up to `elpe`+1 most-recent entries (newest first). Per entry:
`error_count` (monotonic ID), `sqid` / `cmdid` (which queue/command),
`status_field` (the SC/SCT that failed), `lba`, `nsid`, `phase_tag`. On a healthy
idle drive this is all zeroes — it fills in only after a command actually fails,
which is what Project 2 (failure injection) drives.

## Firmware Slot Information — Log Page `0x03`

```
nvme fw-log /dev/nvme0 -o json
```

`afi` (active firmware info: which slot is active / will be next) and `frs1..7`
(firmware revision string per slot).

## Commands Supported and Effects — Log Page `0x05`

```
nvme effects-log /dev/nvme0 -o json
```

One 32-bit entry per opcode. Bit 0 = command supported; other bits say whether
it changes namespace/controller state, needs the namespace quiesced, etc. Useful
for a test harness deciding what it is even allowed to send.

## OCP SMART / Health Extended — Log Page `0xC0`

```
nvme ocp smart-add-log /dev/nvme0 -o json
```

OCP (Open Compute Project) Datacenter NVMe SSD spec defines a vendor log at
`0xC0` that extends standard SMART. Fields it adds beyond `0x02` include:
Physical Media Units Written/Read (128-bit, actual NAND vs host writes → write
amplification), Bad User/System NAND Block counts (raw + normalized), XOR /
Uncorrectable / Soft-ECC error counts, End-to-End correction counts, System Data
% Used, Refresh Counts, min/max User Data Erase Counts, Thermal Throttling
status + event count, PCIe Correctable Error Count, Incapacitated/Incomplete
Shutdowns, % Free Blocks, Capacitor Health (PLP), Endurance Estimate, and a
Log Page GUID that identifies the log as the OCP one.

The standard-vs-OCP comparison, with the values actually observed here, is in
`docs/findings.md`.

## Dataset Management / Trim — opcode `0x09`

```
nvme dsm /dev/nvme0n1 --slbs 0 --blocks 128 --ad
```

`--ad` sets the Attribute-Deallocate bit: tell the drive these LBA ranges are no
longer in use. `--slbs` (starting LBAs) and `--blocks` (lengths) are
comma-separated lists — one DSM command can carry up to 256 ranges. After a
deallocate, `id-ns` `nuse` should drop and reads from the range return the
drive's deallocated pattern (typically zeroes).
