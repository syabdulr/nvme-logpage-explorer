import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pandas as pd
    from analyze_telemetry import evaluate_trends, load_telemetry, trend_summary
    _IMPORT_ERROR = None
except ImportError as exc:  # pandas/numpy are an optional extra, not core deps
    _IMPORT_ERROR = exc

SAMPLE_CSV = Path(__file__).resolve().parent.parent / "samples" / "project1" / "telemetry.csv"


@unittest.skipIf(_IMPORT_ERROR, f"pandas/numpy not installed: {_IMPORT_ERROR}")
class TestTrendSummary(unittest.TestCase):
    def test_flat_committed_sample_has_no_findings(self):
        # samples/project1/telemetry.csv is 10 samples on an idle emulated
        # drive with no workload between them -- every tracked column is
        # constant, so there should be nothing to flag.
        df = load_telemetry(SAMPLE_CSV)
        self.assertEqual(evaluate_trends(df), [])

    def test_summary_reports_zero_slope_on_flat_data(self):
        df = load_telemetry(SAMPLE_CSV)
        summary = trend_summary(df)
        self.assertAlmostEqual(summary.loc["available_spare", "slope_per_hour"], 0.0)

    def test_single_sample_yields_no_findings(self):
        df = load_telemetry(SAMPLE_CSV).iloc[:1]
        self.assertEqual(evaluate_trends(df), [])


@unittest.skipIf(_IMPORT_ERROR, f"pandas/numpy not installed: {_IMPORT_ERROR}")
class TestDegradationDetection(unittest.TestCase):
    def _frame(self, **columns) -> pd.DataFrame:
        n = len(next(iter(columns.values())))
        base = {
            "captured_utc": pd.date_range("2026-01-01", periods=n, freq="h"),
            "available_spare": [100] * n,
            "percentage_used": [0] * n,
            "media_errors": [0] * n,
            "num_err_log_entries": [0] * n,
            "temperature_c": [50] * n,
        }
        base.update(columns)
        return pd.DataFrame(base)

    def test_declining_available_spare_is_flagged_warn(self):
        df = self._frame(available_spare=[100, 90, 80, 70, 60, 50])
        findings = {f.rule: f for f in evaluate_trends(df)}
        self.assertIn("available_spare_trend", findings)
        self.assertEqual(findings["available_spare_trend"].severity, "WARN")

    def test_climbing_media_errors_is_flagged_critical(self):
        df = self._frame(media_errors=[0, 0, 1, 3])
        findings = {f.rule: f for f in evaluate_trends(df)}
        self.assertIn("media_errors_trend", findings)
        self.assertEqual(findings["media_errors_trend"].severity, "CRITICAL")

    def test_stable_columns_are_not_flagged(self):
        df = self._frame(available_spare=[100] * 6)
        self.assertEqual(evaluate_trends(df), [])


if __name__ == "__main__":
    unittest.main()
