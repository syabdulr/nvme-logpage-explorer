#!/usr/bin/env python3
"""
Unit tests for the pure logic in explore.py — decode, normalise, threshold
rules, diff. No device or nvme-cli needed; runs against the committed captures
in samples/project1/.

    python3 -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import explore  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "project1"


class TestCriticalWarning(unittest.TestCase):
    def test_clear(self):
        self.assertEqual(explore.decode_critical_warning(0), [])
        self.assertEqual(explore.decode_critical_warning(None), [])

    def test_bits(self):
        self.assertEqual(explore.decode_critical_warning(0b1),
                         ["spare capacity below threshold"])
        decoded = explore.decode_critical_warning(0b101)
        self.assertIn("spare capacity below threshold", decoded)
        self.assertIn("NVM subsystem reliability degraded", decoded)
        self.assertEqual(len(decoded), 2)


class TestSmartLogPageDecode(unittest.TestCase):
    def test_known_layout(self):
        raw = bytearray(512)
        raw[0] = 0x00                       # critical warning
        raw[1:3] = (323).to_bytes(2, "little")   # 323 K
        raw[3] = 100                        # available spare
        raw[4] = 10                         # threshold
        raw[5] = 7                          # percentage used
        raw[32:48] = (12345).to_bytes(16, "little")  # data units read
        raw[160:176] = (2).to_bytes(16, "little")    # media errors
        d = explore.decode_smart_log_page(bytes(raw))
        self.assertEqual(d["composite_temperature_k"], 323)
        self.assertEqual(d["available_spare"], 100)
        self.assertEqual(d["percentage_used"], 7)
        self.assertEqual(d["data_units_read"], 12345)
        self.assertEqual(d["media_errors"], 2)

    def test_short_page_rejected(self):
        with self.assertRaises(explore.NvmeError):
            explore.decode_smart_log_page(b"\x00" * 100)

    def test_matches_captured_smart_log(self):
        """The generic get-log decode in the capture agreed with `smart-log`."""
        snap = json.loads((SAMPLES / "baseline.json").read_text())
        gl = snap["log_pages"]["smart_via_get_log_page"]
        self.assertTrue(gl["matches_smart_log_command"])
        self.assertEqual(gl["decoded"]["composite_temperature_k"],
                         snap["smart_normalised"]["temperature_k"])


class TestNormalise(unittest.TestCase):
    def test_alias_and_celsius(self):
        s = explore.normalise_smart(
            {"temperature": 313, "avail_spare": 90, "spare_thresh": 5,
             "percent_used": 3, "media_errors": 0})
        self.assertEqual(s["temperature_k"], 313)
        self.assertEqual(s["temperature_c"], 40)
        self.assertEqual(s["available_spare"], 90)
        self.assertEqual(s["available_spare_threshold"], 5)
        self.assertEqual(s["percentage_used"], 3)


class TestThresholdRules(unittest.TestCase):
    def _snap(self, **smart):
        base = {"critical_warning": 0, "available_spare": 100,
                "available_spare_threshold": 10, "percentage_used": 0,
                "temperature_c": 40, "media_errors": 0, "num_err_log_entries": 0}
        base.update(smart)
        return {"smart_normalised": base}

    def test_healthy(self):
        f = explore.evaluate(self._snap(), explore.Thresholds())
        self.assertEqual(f, [])

    def test_spare_zero_zero_is_info_not_critical(self):
        f = explore.evaluate(
            self._snap(available_spare=0, available_spare_threshold=0),
            explore.Thresholds())
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "INFO")

    def test_spare_below_threshold_is_critical(self):
        f = explore.evaluate(
            self._snap(available_spare=8, available_spare_threshold=10),
            explore.Thresholds())
        self.assertTrue(any(x.rule == "available_spare" and x.severity == "CRITICAL"
                            for x in f))

    def test_critical_warning_bit(self):
        f = explore.evaluate(self._snap(critical_warning=0b10),
                             explore.Thresholds())
        self.assertTrue(any(x.rule == "critical_warning" and x.severity == "CRITICAL"
                            for x in f))

    def test_endurance_warn_and_critical(self):
        warn = explore.evaluate(self._snap(percentage_used=95),
                                explore.Thresholds())
        self.assertTrue(any(x.severity == "WARN" and x.rule == "percentage_used"
                            for x in warn))
        crit = explore.evaluate(self._snap(percentage_used=100),
                                explore.Thresholds())
        self.assertTrue(any(x.severity == "CRITICAL" and x.rule == "percentage_used"
                            for x in crit))

    def test_temperature_warn(self):
        f = explore.evaluate(self._snap(temperature_c=75),
                             explore.Thresholds(temp_warn_c=70))
        self.assertTrue(any(x.rule == "temperature" for x in f))


class TestDiffFlatten(unittest.TestCase):
    def test_flatten_nested(self):
        flat = explore._flatten({"a": {"b": [1, 2]}, "c": 3})
        self.assertEqual(flat["a.b[0]"], 1)
        self.assertEqual(flat["a.b[1]"], 2)
        self.assertEqual(flat["c"], 3)

    def test_captured_workload_moved_counters(self):
        before = json.loads((SAMPLES / "baseline.json").read_text())
        after = json.loads((SAMPLES / "after.json").read_text())
        b = explore._flatten(before["log_pages"])
        a = explore._flatten(after["log_pages"])
        self.assertEqual(b["smart_health.data_units_written"], 0)
        self.assertGreater(a["smart_health.data_units_written"], 0)
        self.assertEqual(
            a["ocp_smart_health_extended.Physical media units written.lo"],
            32 * 1024 * 1024)


class TestLibraryApi(unittest.TestCase):
    def test_compute_diff_returns_changes(self):
        before = json.loads((SAMPLES / "baseline.json").read_text())
        after = json.loads((SAMPLES / "after.json").read_text())
        changes = explore.compute_diff(before, after)
        self.assertTrue(changes)
        by_path = {c.path: c for c in changes}
        w = by_path["smart_health.data_units_written"]
        self.assertEqual(w.before, 0)
        self.assertEqual(w.delta, w.after - w.before)
        self.assertIn("path", w.as_dict())

    def test_evaluate_default_thresholds(self):
        snap = json.loads((SAMPLES / "baseline.json").read_text())
        findings = explore.evaluate(snap)  # no Thresholds arg
        self.assertTrue(all(hasattr(f, "severity") for f in findings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
