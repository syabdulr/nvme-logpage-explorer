#!/usr/bin/env python3
"""
Trend analysis over a telemetry poll CSV (see `explore.py poll`).

explore.py's `check` / `evaluate` are point-in-time threshold rules over a
single snapshot. This adds pandas/numpy trend analysis across a *window* of
polls, catching slow degradation a single sample can't see on its own — e.g.
available_spare drifting down or media_errors climbing across the poll
window, even if no individual sample crosses a threshold.

    python3 analyze_telemetry.py output/telemetry.csv
    python3 analyze_telemetry.py output/telemetry.csv --json

Requires pandas + numpy: pip install -r requirements-analysis.txt
(explore.py itself stays stdlib-only; this is a separate, optional tool.)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from explore import Finding

# Column -> direction that counts as degradation.
TREND_COLUMNS = {
    "available_spare": "down",
    "percentage_used": "up",
    "media_errors": "up",
    "num_err_log_entries": "up",
    "temperature_c": "up",
}

# Minimum |slope| (units per hour) before a trend is called out rather than
# treated as noise on a short poll window.
SLOPE_THRESHOLDS = {
    "available_spare": 0.5,
    "percentage_used": 0.5,
    "media_errors": 1e-9,        # any sustained upward slope matters
    "num_err_log_entries": 1e-9,
    "temperature_c": 2.0,
}

CRITICAL_COLUMNS = {"media_errors", "num_err_log_entries"}


def load_telemetry(csv_path: str | Path) -> pd.DataFrame:
    """Load a CSV written by `explore.py poll --csv ...`, oldest sample first."""
    df = pd.read_csv(csv_path, parse_dates=["captured_utc"])
    return df.sort_values("captured_utc").reset_index(drop=True)


def _slope_per_hour(df: pd.DataFrame, column: str) -> float:
    elapsed_h = (df["captured_utc"] - df["captured_utc"].iloc[0]).dt.total_seconds() / 3600.0
    series = df[column].astype(float)
    if elapsed_h.iloc[-1] == 0 or series.nunique() <= 1:
        return 0.0
    slope, _intercept = np.polyfit(elapsed_h, series, 1)
    return float(slope)


def trend_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Per-column first/last/delta/slope/min/max/mean over the poll window."""
    rows = []
    for column in TREND_COLUMNS:
        if column not in df.columns:
            continue
        series = df[column].astype(float)
        rows.append({
            "column": column,
            "first": series.iloc[0],
            "last": series.iloc[-1],
            "delta": series.iloc[-1] - series.iloc[0],
            "slope_per_hour": _slope_per_hour(df, column),
            "min": series.min(),
            "max": series.max(),
            "mean": series.mean(),
        })
    return pd.DataFrame(rows).set_index("column")


def evaluate_trends(df: pd.DataFrame) -> list[Finding]:
    """Flag columns whose slope over the window exceeds its noise threshold."""
    findings: list[Finding] = []
    if len(df) < 2:
        return findings

    summary = trend_summary(df)
    for column, bad_direction in TREND_COLUMNS.items():
        if column not in summary.index:
            continue
        slope = summary.loc[column, "slope_per_hour"]
        threshold = SLOPE_THRESHOLDS.get(column, 0.0)
        is_bad = (bad_direction == "down" and slope < -threshold) or \
                 (bad_direction == "up" and slope > threshold)
        if not is_bad:
            continue
        severity = "CRITICAL" if column in CRITICAL_COLUMNS else "WARN"
        first, last = summary.loc[column, "first"], summary.loc[column, "last"]
        findings.append(Finding(
            severity, f"{column}_trend",
            f"{column} trending {bad_direction} at {slope:+.4f}/hr across "
            f"{len(df)} samples ({first:g} -> {last:g})",
            {"slope_per_hour": slope, "delta": float(summary.loc[column, "delta"])},
        ))
    return findings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("csv", help="telemetry CSV produced by `explore.py poll --csv ...`")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = p.parse_args(argv)

    df = load_telemetry(args.csv)
    summary = trend_summary(df)
    findings = evaluate_trends(df)
    worst_critical = any(f.severity == "CRITICAL" for f in findings)

    if args.json:
        print(json.dumps({
            "samples": len(df),
            "window": {
                "start": df["captured_utc"].iloc[0].isoformat(),
                "end": df["captured_utc"].iloc[-1].isoformat(),
            },
            "summary": json.loads(summary.reset_index().to_json(orient="records")),
            "findings": [asdict(f) for f in findings],
        }, indent=2))
        return 1 if worst_critical else 0

    print(f"{len(df)} samples, {df['captured_utc'].iloc[0]} -> {df['captured_utc'].iloc[-1]}\n")
    print(summary.round(4).to_string())
    print()
    if not findings:
        print("no adverse trends detected")
    for f in findings:
        print(f"[{f.severity}] {f.rule}: {f.message}")
    return 1 if worst_critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
