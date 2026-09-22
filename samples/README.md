# Curated captures

Reference output committed so the findings in [`docs/findings.md`](../docs/findings.md)
are reproducible without standing up the VM. Regenerate with:

```
env/up.sh -- python3 explore.py snapshot -o samples/project1/baseline.json
```

`project1/` — snapshots and diffs from the Command & Log Page Explorer runs,
plus `telemetry.csv` / `telemetry_degrading_synthetic.csv` used by
[`analyze_telemetry.py`](../analyze_telemetry.py) (see the
[top-level README](../README.md)'s trend-analysis section).
