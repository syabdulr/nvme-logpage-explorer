#!/usr/bin/env python3
"""
NVMe Command & Log Page Explorer.

Shells out to nvme-cli, normalises the JSON, and builds the tooling an SSD
monitoring / qualification pipeline needs on top of it:

    snapshot   pull Identify + every relevant log page into one JSON document
    poll       sample SMART + OCP health on an interval into SQLite / CSV
    diff       structural + numeric diff of two snapshots
    check      threshold rules over a snapshot (spare, wear, temp, errors)

Runs against any device nvme-cli can see. In this project that device is a
QEMU-emulated NVMe controller with the OCP feature set enabled (see env/).

stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TOOL_VERSION = "0.1.0"


# --------------------------------------------------------------------------- #
# nvme-cli plumbing
# --------------------------------------------------------------------------- #
class NvmeError(RuntimeError):
    pass


def _nvme_bin() -> str:
    path = shutil.which("nvme")
    if not path:
        raise NvmeError("nvme-cli not found on PATH (apt install nvme-cli)")
    return path


def _need_sudo(requested: bool) -> bool:
    # nvme admin commands need CAP_SYS_ADMIN; skip the sudo hop if already root.
    return requested and os.geteuid() != 0 and shutil.which("sudo") is not None


def _cmd(args: list[str], *, use_sudo: bool) -> list[str]:
    cmd: list[str] = []
    if _need_sudo(use_sudo):
        cmd.append("sudo")
    cmd.append(_nvme_bin())
    cmd.extend(args)
    return cmd


def _run(args: list[str], *, use_sudo: bool = False) -> str:
    cmd = _cmd(args, use_sudo=use_sudo)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise NvmeError(
            f"`{' '.join(cmd)}` exited {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def _run_bytes(args: list[str], *, use_sudo: bool = True) -> bytes:
    cmd = _cmd(args, use_sudo=use_sudo)
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise NvmeError(
            f"`{' '.join(cmd)}` exited {proc.returncode}: "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def nvme_json(args: list[str], *, use_sudo: bool = True) -> Any:
    """Run an nvme-cli subcommand with `-o json` and parse the result."""
    out = _run([*args, "-o", "json"], use_sudo=use_sudo)
    out = out.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise NvmeError(f"could not parse JSON from `nvme {' '.join(args)}`: {exc}")


def nvme_version() -> str:
    try:
        return _run(["version"]).strip().splitlines()[0]
    except NvmeError:
        return "unknown"


# --------------------------------------------------------------------------- #
# device discovery
# --------------------------------------------------------------------------- #
@dataclass
class Device:
    controller: str  # /dev/nvme0
    namespace: str   # /dev/nvme0n1
    model: str = ""
    serial: str = ""
    firmware: str = ""


def _walk_list_json(doc: Any) -> Iterable[dict]:
    """Yield namespace records from either nvme-cli list JSON schema."""
    devices = doc.get("Devices", []) if isinstance(doc, dict) else []
    for entry in devices:
        # newer schema: Devices[].Subsystems[].Controllers[].Namespaces[]
        for subsys in entry.get("Subsystems", []) or []:
            for ctrl in subsys.get("Controllers", []) or []:
                for ns in ctrl.get("Namespaces", []) or []:
                    yield {
                        "controller": ctrl.get("Controller", ""),
                        "ns": ns.get("NameSpace") or ns.get("Namespace", ""),
                        "model": ctrl.get("ModelNumber") or subsys.get("ModelNumber", ""),
                        "serial": ctrl.get("SerialNumber", ""),
                        "firmware": ctrl.get("Firmware", ""),
                    }
        # older/flat schema: Devices[] is the namespace list
        if "DevicePath" in entry:
            dp = entry["DevicePath"]
            yield {
                "controller": dp.rsplit("n", 1)[0],
                "ns": dp,
                "model": entry.get("ModelNumber", ""),
                "serial": entry.get("SerialNumber", ""),
                "firmware": entry.get("Firmware", ""),
            }


def discover(controller: str | None, namespace: str | None) -> Device:
    if controller and namespace:
        return Device(controller=controller, namespace=namespace)
    try:
        records = list(_walk_list_json(nvme_json(["list"])))
    except NvmeError:
        records = []
    if not records:
        # last resort: assume the canonical first device
        return Device(controller=controller or "/dev/nvme0",
                      namespace=namespace or "/dev/nvme0n1")
    rec = records[0]
    ns = rec["ns"] if rec["ns"].startswith("/dev/") else f"/dev/{rec['ns']}"
    ctrl = rec["controller"]
    ctrl = ctrl if ctrl.startswith("/dev/") else f"/dev/{ctrl}"
    return Device(
        controller=controller or ctrl,
        namespace=namespace or ns,
        model=rec["model"].strip(),
        serial=rec["serial"].strip(),
        firmware=rec["firmware"].strip(),
    )


# --------------------------------------------------------------------------- #
# collectors
# --------------------------------------------------------------------------- #
def collect_identify_controller(dev: Device) -> Any:
    return nvme_json(["id-ctrl", dev.controller])


def collect_identify_namespace(dev: Device) -> Any:
    return nvme_json(["id-ns", dev.namespace])


def collect_smart(dev: Device) -> Any:
    return nvme_json(["smart-log", dev.controller])


def collect_error_log(dev: Device) -> Any:
    return nvme_json(["error-log", dev.controller])


def collect_fw_log(dev: Device) -> Any:
    return nvme_json(["fw-log", dev.controller])


def collect_effects_log(dev: Device) -> Any:
    return nvme_json(["effects-log", dev.controller])


def collect_ocp_smart(dev: Device) -> Any:
    """OCP 0xC0 SMART / Health Extended. Absent if the plugin/feature is missing."""
    for args in (["ocp", "smart-add-log", dev.controller],
                 ["ocp", "smart-add-log", "-d", dev.controller]):
        try:
            return nvme_json(args)
        except NvmeError:
            continue
    return {"_unavailable": "nvme ocp smart-add-log not supported by device/plugin"}


def _u(b: bytes) -> int:
    return int.from_bytes(b, "little")


def decode_smart_log_page(raw: bytes) -> dict:
    """Decode the fields of the 512-byte SMART/Health log page (LID 0x02)."""
    if len(raw) < 512:
        raise NvmeError(f"short SMART log page: {len(raw)} bytes")
    return {
        "critical_warning": raw[0],
        "composite_temperature_k": _u(raw[1:3]),
        "available_spare": raw[3],
        "available_spare_threshold": raw[4],
        "percentage_used": raw[5],
        "data_units_read": _u(raw[32:48]),
        "data_units_written": _u(raw[48:64]),
        "host_read_commands": _u(raw[64:80]),
        "host_write_commands": _u(raw[80:96]),
        "power_on_hours": _u(raw[128:144]),
        "media_errors": _u(raw[160:176]),
        "num_err_log_entries": _u(raw[176:192]),
    }


def collect_generic_getlog_smart(dev: Device) -> Any:
    """
    Pull LID 0x02 via the *generic* Get Log Page path (raw bytes), decode it
    here, and confirm it agrees with the `smart-log` convenience command —
    they are the same log page.
    """
    try:
        raw = _run_bytes(["get-log", dev.controller,
                          "--log-id", "0x02", "--log-len", "512", "--raw-binary"])
    except NvmeError as exc:
        return {"_error": str(exc)}
    try:
        decoded = decode_smart_log_page(raw)
    except NvmeError as exc:
        return {"_error": str(exc), "raw_first_16_hex": raw[:16].hex(" ")}

    result = {
        "method": "nvme get-log --log-id 0x02 --log-len 512 --raw-binary",
        "raw_first_48_hex": raw[:48].hex(" "),
        "decoded": decoded,
    }
    try:
        smart = normalise_smart(collect_smart(dev))
        agree = (
            decoded["critical_warning"] == smart.get("critical_warning")
            and decoded["composite_temperature_k"] == smart.get("temperature_k")
            and decoded["percentage_used"] == smart.get("percentage_used")
            and decoded["data_units_read"] == smart.get("data_units_read")
        )
        result["matches_smart_log_command"] = bool(agree)
    except NvmeError:
        pass
    return result


COLLECTORS = {
    "identify_controller": collect_identify_controller,
    "identify_namespace": collect_identify_namespace,
    "smart_health": collect_smart,
    "smart_via_get_log_page": collect_generic_getlog_smart,
    "error_information": collect_error_log,
    "firmware_slot": collect_fw_log,
    "commands_supported_effects": collect_effects_log,
    "ocp_smart_health_extended": collect_ocp_smart,
}


# --------------------------------------------------------------------------- #
# field access (nvme-cli JSON key names drift across versions)
# --------------------------------------------------------------------------- #
def pick(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


SMART_ALIASES = {
    "critical_warning": ("critical_warning",),
    "temperature_k": ("temperature", "temperature_k"),
    "available_spare": ("avail_spare", "available_spare"),
    "available_spare_threshold": ("spare_thresh", "available_spare_threshold"),
    "percentage_used": ("percent_used", "percentage_used"),
    "data_units_read": ("data_units_read",),
    "data_units_written": ("data_units_written",),
    "host_read_commands": ("host_read_commands",),
    "host_write_commands": ("host_write_commands",),
    "power_cycles": ("power_cycles",),
    "power_on_hours": ("power_on_hours",),
    "unsafe_shutdowns": ("unsafe_shutdowns",),
    "media_errors": ("media_errors",),
    "num_err_log_entries": ("num_err_log_entries",),
    "controller_busy_time": ("controller_busy_time",),
    "warning_temp_time": ("warning_temp_time",),
    "critical_comp_time": ("critical_comp_time",),
}

# fields tracked as columns by `poll`
POLL_FIELDS = [
    "critical_warning", "temperature_c", "available_spare",
    "available_spare_threshold", "percentage_used",
    "data_units_read", "data_units_written",
    "host_read_commands", "host_write_commands",
    "power_on_hours", "unsafe_shutdowns", "media_errors",
    "num_err_log_entries", "controller_busy_time",
]


def normalise_smart(raw: dict) -> dict:
    out: dict[str, Any] = {}
    for canon, aliases in SMART_ALIASES.items():
        out[canon] = pick(raw, *aliases)
    temp_k = out.get("temperature_k")
    out["temperature_c"] = (temp_k - 273) if isinstance(temp_k, (int, float)) else None
    return out


CRITICAL_WARNING_BITS = {
    0: "spare capacity below threshold",
    1: "temperature outside operating range",
    2: "NVM subsystem reliability degraded",
    3: "media placed in read-only mode",
    4: "volatile memory backup device failed",
    5: "persistent memory region read-only/unreliable",
}


def decode_critical_warning(value: int | None) -> list[str]:
    if not value:
        return []
    return [text for bit, text in CRITICAL_WARNING_BITS.items() if value & (1 << bit)]


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def build_snapshot(dev: Device, sections: Iterable[str]) -> dict:
    snap: dict[str, Any] = {
        "meta": {
            "tool": "nvme-logpage-explorer",
            "tool_version": TOOL_VERSION,
            "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "nvme_cli": nvme_version(),
            "device": {
                "controller": dev.controller,
                "namespace": dev.namespace,
                "model": dev.model,
                "serial": dev.serial,
                "firmware": dev.firmware,
            },
        },
        "log_pages": {},
    }
    for name in sections:
        collector = COLLECTORS[name]
        try:
            snap["log_pages"][name] = collector(dev)
        except NvmeError as exc:
            snap["log_pages"][name] = {"_error": str(exc)}
    raw_smart = snap["log_pages"].get("smart_health")
    if isinstance(raw_smart, dict) and "_error" not in raw_smart:
        snap["smart_normalised"] = normalise_smart(raw_smart)
        snap["smart_normalised"]["critical_warning_decoded"] = decode_critical_warning(
            snap["smart_normalised"].get("critical_warning")
        )
    return snap


def cmd_snapshot(args: argparse.Namespace) -> int:
    dev = discover(args.device, args.namespace)
    sections = args.sections or list(COLLECTORS)
    snap = build_snapshot(dev, sections)
    text = json.dumps(snap, indent=2)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
        print(f"wrote {out}  ({len(sections)} sections, device {dev.namespace})")
    else:
        print(text)
    return 0


# --------------------------------------------------------------------------- #
# poll
# --------------------------------------------------------------------------- #
POLL_SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_utc  TEXT NOT NULL,
    device        TEXT NOT NULL,
    {columns}
);
"""


def _poll_row(dev: Device) -> dict:
    smart = normalise_smart(collect_smart(dev))
    row = {"captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "device": dev.namespace}
    for f in POLL_FIELDS:
        row[f] = smart.get(f)
    return row


def cmd_poll(args: argparse.Namespace) -> int:
    dev = discover(args.device, args.namespace)
    db_path = Path(args.database)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(POLL_SCHEMA.format(
        columns=",\n    ".join(f"{c} INTEGER" for c in POLL_FIELDS)))
    conn.commit()

    csv_writer = None
    csv_fh = None
    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        csv_fh = open(args.csv, "w", newline="")
        csv_writer = csv.DictWriter(
            csv_fh, fieldnames=["captured_utc", "device", *POLL_FIELDS])
        csv_writer.writeheader()

    cols = ["captured_utc", "device", *POLL_FIELDS]
    placeholders = ",".join("?" for _ in cols)
    insert = f"INSERT INTO smart_samples ({','.join(cols)}) VALUES ({placeholders})"

    print(f"polling {dev.namespace} every {args.interval}s -> {db_path}"
          f"{' + ' + args.csv if args.csv else ''}  (Ctrl-C to stop)")
    prev: dict | None = None
    taken = 0
    try:
        while args.count == 0 or taken < args.count:
            row = _poll_row(dev)
            conn.execute(insert, [row[c] for c in cols])
            conn.commit()
            if csv_writer:
                csv_writer.writerow(row)
                csv_fh.flush()
            deltas = ""
            if prev is not None:
                dm = _delta(prev, row, "media_errors")
                de = _delta(prev, row, "num_err_log_entries")
                ds = _delta(prev, row, "available_spare")
                flags = []
                if dm:
                    flags.append(f"media_errors +{dm}")
                if de:
                    flags.append(f"err_log +{de}")
                if ds and ds < 0:
                    flags.append(f"spare {ds}")
                deltas = "  " + ", ".join(flags) if flags else ""
            print(f"  {row['captured_utc']}  used={row['percentage_used']}% "
                  f"spare={row['available_spare']}% temp={row['temperature_c']}C "
                  f"media_err={row['media_errors']} err_log={row['num_err_log_entries']}"
                  f"{deltas}")
            prev = row
            taken += 1
            if args.count and taken >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        conn.close()
        if csv_fh:
            csv_fh.close()
    print(f"{taken} samples stored in {db_path}")
    return 0


def _delta(a: dict, b: dict, key: str):
    x, y = a.get(key), b.get(key)
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return y - x
    return None


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flat.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        flat[prefix] = obj
    return flat


def cmd_diff(args: argparse.Namespace) -> int:
    a = json.loads(Path(args.before).read_text())
    b = json.loads(Path(args.after).read_text())
    fa = _flatten(a.get("log_pages", a))
    fb = _flatten(b.get("log_pages", b))
    keys = sorted(set(fa) | set(fb))

    changed: list[tuple[str, Any, Any]] = []
    for k in keys:
        if k.startswith("meta") or "captured_utc" in k:
            continue
        va, vb = fa.get(k, "<absent>"), fb.get(k, "<absent>")
        if va != vb:
            changed.append((k, va, vb))

    if not changed:
        print("no differences outside metadata")
        return 0

    print(f"{len(changed)} field(s) changed  ({args.before} -> {args.after})\n")
    for k, va, vb in changed:
        delta = ""
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            delta = f"   (Δ {vb - va:+})"
        print(f"  {k}")
        print(f"      {va}  ->  {vb}{delta}")
    return 0


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
SEVERITIES = {"INFO": 0, "WARN": 1, "CRITICAL": 2}


@dataclass
class Finding:
    severity: str
    rule: str
    message: str
    observed: Any = None


@dataclass
class Thresholds:
    used_warn: int = 90
    used_critical: int = 100
    temp_warn_c: int = 70
    spare_margin: int = 0  # extra points above the drive's own threshold to warn at


def evaluate(snap: dict, t: Thresholds) -> list[Finding]:
    s = snap.get("smart_normalised") or normalise_smart(
        snap.get("log_pages", {}).get("smart_health", {}))
    findings: list[Finding] = []

    cw = s.get("critical_warning")
    for text in decode_critical_warning(cw):
        findings.append(Finding("CRITICAL", "critical_warning", text, cw))

    spare = s.get("available_spare")
    thr = s.get("available_spare_threshold")
    if spare == 0 and thr == 0:
        # QEMU and some real drives report 0/0 to mean "spare not tracked"
        findings.append(Finding(
            "INFO", "available_spare",
            "available spare and threshold both 0 — device does not report spare",
            spare))
    elif isinstance(spare, int) and isinstance(thr, int) and thr > 0:
        if spare <= thr:
            findings.append(Finding(
                "CRITICAL", "available_spare",
                f"available spare {spare}% at/below drive threshold {thr}%", spare))
        elif spare <= thr + t.spare_margin:
            findings.append(Finding(
                "WARN", "available_spare",
                f"available spare {spare}% within {t.spare_margin} pts of "
                f"threshold {thr}%", spare))

    used = s.get("percentage_used")
    if isinstance(used, int):
        if used >= t.used_critical:
            findings.append(Finding(
                "CRITICAL", "percentage_used",
                f"endurance used {used}% ≥ {t.used_critical}%", used))
        elif used >= t.used_warn:
            findings.append(Finding(
                "WARN", "percentage_used",
                f"endurance used {used}% ≥ {t.used_warn}%", used))

    temp = s.get("temperature_c")
    if isinstance(temp, (int, float)) and temp >= t.temp_warn_c:
        findings.append(Finding(
            "WARN", "temperature",
            f"composite temperature {temp}°C ≥ {t.temp_warn_c}°C", temp))

    media = s.get("media_errors")
    if isinstance(media, int) and media > 0:
        findings.append(Finding(
            "WARN", "media_errors",
            f"{media} media/data-integrity error(s) reported", media))

    errlog = s.get("num_err_log_entries")
    if isinstance(errlog, int) and errlog > 0:
        findings.append(Finding(
            "INFO", "num_err_log_entries",
            f"{errlog} error-log entr(y/ies) present — inspect error_information",
            errlog))

    return findings


def cmd_check(args: argparse.Namespace) -> int:
    if args.snapshot == "-":
        dev = discover(args.device, args.namespace)
        snap = build_snapshot(dev, ["smart_health", "error_information"])
    else:
        snap = json.loads(Path(args.snapshot).read_text())

    t = Thresholds(
        used_warn=args.used_warn, used_critical=args.used_critical,
        temp_warn_c=args.temp_warn, spare_margin=args.spare_margin)
    findings = sorted(evaluate(snap, t),
                      key=lambda f: -SEVERITIES[f.severity])

    dev_str = snap.get("meta", {}).get("device", {}).get("namespace", "device")
    if not findings:
        print(f"OK  {dev_str}: no threshold rule triggered")
        return 0

    worst = max(SEVERITIES[f.severity] for f in findings)
    verdict = {0: "OK", 1: "WARN", 2: "CRITICAL"}[worst]
    print(f"{verdict}  {dev_str}: {len(findings)} finding(s)\n")
    for f in findings:
        print(f"  [{f.severity:8}] {f.rule}: {f.message}")
    # exit code = worst severity (0 OK/INFO, 1 WARN, 2 CRITICAL) for CI gating
    return worst


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="explore.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", help="controller node, e.g. /dev/nvme0 (default: autodetect)")
    p.add_argument("--namespace", help="namespace node, e.g. /dev/nvme0n1 (default: autodetect)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("snapshot", help="capture Identify + log pages to JSON")
    sp.add_argument("-o", "--output", help="write here instead of stdout")
    sp.add_argument("--sections", nargs="+", choices=list(COLLECTORS),
                    help="limit to these sections (default: all)")
    sp.set_defaults(func=cmd_snapshot)

    pp = sub.add_parser("poll", help="sample SMART on an interval into SQLite/CSV")
    pp.add_argument("--interval", type=float, default=5.0, help="seconds between samples")
    pp.add_argument("--count", type=int, default=0, help="stop after N samples (0 = forever)")
    pp.add_argument("--database", default="output/telemetry.sqlite")
    pp.add_argument("--csv", help="also append rows to this CSV")
    pp.set_defaults(func=cmd_poll)

    dp = sub.add_parser("diff", help="diff two snapshot JSON files")
    dp.add_argument("before")
    dp.add_argument("after")
    dp.set_defaults(func=cmd_diff)

    cp = sub.add_parser("check", help="run threshold rules over a snapshot ('-' = live)")
    cp.add_argument("snapshot")
    cp.add_argument("--used-warn", type=int, default=90)
    cp.add_argument("--used-critical", type=int, default=100)
    cp.add_argument("--temp-warn", type=int, default=70)
    cp.add_argument("--spare-margin", type=int, default=0)
    cp.set_defaults(func=cmd_check)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except NvmeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
