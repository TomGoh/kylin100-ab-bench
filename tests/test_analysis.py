import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from abbench.analysis import analyze_capture, memory_counters, verified_boottime
from test_supply import raw as supply_raw
from abbench.supply import parse_supply_output


class AnalysisTests(unittest.TestCase):
    def test_trace_clock_must_match_boottime_values_and_exist(self):
        self.assertTrue(verified_boottime('ts,clock_id,clock_name,clock_value\n100,6,BOOTTIME,100\n'))
        self.assertFalse(verified_boottime('ts,clock_id,clock_name,clock_value\n100,6,BOOTTIME,90\n'))
        self.assertFalse(verified_boottime('ts,clock_id,clock_name,clock_value\n'))
        self.assertFalse(verified_boottime('name\nnot-a-clock\n'))

    def test_memory_uses_bytes_and_calls_minimum_sampled(self):
        rows = [{"type": "meminfo", "name": name, "unit": "bytes", "ts_ns": str(t), "value": str(v)}
                for t, a in [(0, 500), (5_000_000_000, 400)] for name, v in [("MemAvailable", a), ("MemTotal", 1000)]]
        result = memory_counters(rows)
        self.assertTrue(result["valid"])
        self.assertEqual(result["estimated_unavailable_mean_bytes"], 550)
        self.assertEqual(result["mem_available_sampled_min_bytes"], 400)
        self.assertFalse(result["samples_independent"])
        rows[-1]["unit"] = "kB"
        self.assertFalse(memory_counters(rows)["valid"])

    def test_incomplete_duplicate_or_impossible_memory_is_rejected(self):
        row = {"type": "meminfo", "name": "MemAvailable", "unit": "bytes", "ts_ns": "1", "value": "500"}
        self.assertFalse(memory_counters([row])["valid"])
        self.assertFalse(memory_counters([row, row])["valid"])
        self.assertFalse(memory_counters([row, dict(row, name="MemTotal", value="100")])["valid"])

    def test_requested_memory_window_reports_only_observed_subset(self):
        rows = [{"type": "meminfo", "name": name, "unit": "bytes", "ts_ns": str(t), "value": str(v)}
                for t, a in [(101_000_000_000, 500), (102_000_000_000, 400)]
                for name, v in [("MemAvailable", a), ("MemTotal", 1000)]]
        result = memory_counters(rows, 100, 200, capture_start_s=100, capture_end_s=200)
        self.assertTrue(result["valid"])
        self.assertEqual(result["actual_sampled_window_start_s"], 101)
        self.assertEqual(result["actual_sampled_window_end_s"], 102)
        self.assertEqual(result["actual_sample_gap_max_s"], 1)
        self.assertEqual(result["requested_window_end_s"], 200)
        self.assertFalse(result["requested_edges_sampled"])
        self.assertEqual(result["measurement_boundary"], "android_kernel_visible_memory_sampled_subwindow")

    def test_memory_invalid_time_and_outer_batch_cannot_be_hidden_by_clip(self):
        pair = [{"type": "meminfo", "name": name, "unit": "bytes", "ts_ns": "1000000000", "value": "500"}
                for name in ("MemAvailable", "MemTotal")]
        for timestamp in ("-1", "NaN", 1.5, True):
            with self.subTest(timestamp=timestamp):
                self.assertFalse(memory_counters([dict(row, ts_ns=timestamp) for row in pair])["valid"])
        reversed_rows = [dict(row, ts_ns="2000000000") for row in pair] + pair
        self.assertEqual(memory_counters(reversed_rows)["reason"], "non_increasing_memory_time")
        outside = pair + [dict(row, ts_ns="11000000000") for row in pair]
        self.assertEqual(memory_counters(outside, 10, 12, capture_start_s=10, capture_end_s=12)["reason"],
                         "memory_outside_same_boot_capture_bounds")


class CaptureAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.capture = {"state": "exported", "serial": "test-tablet", "trace": "fake-trace",
                        "before": {"boot_id": "11111111-1111-4111-8111-111111111111", "t_s": 100,
                                   "clock": "proc_uptime_boottime"},
                        "after": {"boot_id": "11111111-1111-4111-8111-111111111111", "t_s": 200,
                                  "clock": "proc_uptime_boottime"}}
        self.profile = {"serial": "test-tablet", "units_validated": True,
                        "current_unit": "uA", "voltage_unit": "uV", "charge_unit": "uAh",
                        "units_validation_reference": "unit-binding-evidence.json",
                        "current_sign_validated": True, "discharge_sign_candidate": -1,
                        "current_sign_reference": "sign-evidence.json"}
        self.clock = "ts,clock_id,clock_name,clock_value\n110000000000,6,BOOTTIME,110000000000\n"
        self.rows = []
        for t in range(110, 121):
            for track_id, name, value in ((2, "batt.current_ua", -500000), (4, "batt.voltage_uv", 4000000)):
                self.rows.append(self.point(t, track_id, name, "battery_counter", "[NULL]", value))
        for t in (110, 115, 120):
            self.rows += [self.point(t, 5, "MemTotal", "meminfo", "bytes", 1000),
                          self.point(t, 6, "MemAvailable", "meminfo", "bytes", 400)]

    @staticmethod
    def point(t, track_id, name, kind, unit, value):
        return {"ts_ns": str(t * 1000000000), "track_id": str(track_id), "name": name,
                "type": kind, "unit": unit, "source_arg_set_id": "0", "dimension_arg_set_id": "0", "value": str(value)}

    def analyze(self, *, profile=None, capture=None, rows=None, clock=None, start=None, end=None):
        supplied_rows = self.rows if rows is None else rows
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "capture.json").write_text(json.dumps(self.capture if capture is None else capture))

            def export(trace, processor, directory):
                directory.mkdir(parents=True)
                with (directory / "counter-samples.csv").open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
                    writer.writeheader()
                    writer.writerows(sorted(supplied_rows, key=lambda row: (int(row["ts_ns"]), int(row["track_id"]))))
                return {"measurement_validated": False}

            with patch("abbench.analysis.export_trace", side_effect=export), patch(
                    "abbench.analysis.subprocess.run", return_value=SimpleNamespace(
                        stdout=self.clock if clock is None else clock, stderr="")):
                result = analyze_capture(target, "not-called", profile=self.profile if profile is None else profile,
                                         start_s=start, end_s=end)
            normalized_file = target / "analysis/normalized-battery.json"
            normalized = json.loads(normalized_file.read_text()) if normalized_file.is_file() else None
            return result, normalized

    def test_auxiliary_roles_are_bound_in_analysis_without_ignoring_online_sources(self):
        extra="NODE\t/sys/class/power_supply/gauge\tUnknown\t?\t1\n"
        capture=copy.deepcopy(self.capture)
        capture.update(physical_serial="test-tablet",supply_before={"raw":supply_raw(extra=extra)},
                       supply_after={"raw":supply_raw(121,122,extra=extra)})
        profile=dict(self.profile,supply_inventory_sha256=parse_supply_output(capture["supply_before"]["raw"])["supply_inventory_sha256"],
                     auxiliary_supply_paths=[{"path":"/sys/class/power_supply/gauge","type":"Unknown",
                                              "role":"battery_gauge","evidence_reference":"role-properties.txt"}])
        result,_=self.analyze(capture=capture,profile=profile)
        self.assertEqual(result["power"]["power_boundary"],"battery_side_device")
        self.assertEqual(result["power"]["energy_j"],20)
        result,_=self.analyze(capture=capture)
        self.assertEqual(result["power"]["power_boundary"],"battery_net")
        self.assertEqual(result["supply_evidence"]["reason"],"external_supply_state_unknown")

    def test_verified_supply_and_physical_identity_allow_known_device_energy(self):
        capture=copy.deepcopy(self.capture)
        capture.update(serial="192.0.2.1:5555",physical_serial="test-tablet",
                       supply_before={"raw":supply_raw()},supply_after={"raw":supply_raw(121,122)})
        result,normalized=self.analyze(capture=capture)
        self.assertTrue(result["power"]["valid"])
        self.assertEqual(result["power"]["power_boundary"],"battery_side_device")
        self.assertEqual(result["power"]["energy_j"],20)
        self.assertTrue(all(s["external_online"] is False for s in normalized["samples"]))
        self.assertFalse(result["power"]["sensor_calibrated"])

    def test_static_isolation_flags_wifi_and_bad_evidence_do_not_upgrade(self):
        capture=copy.deepcopy(self.capture)
        capture.update(serial="192.0.2.1:5555",physical_serial="test-tablet")
        profile=dict(self.profile,input_supply_verified_off=True)
        for pre,post in [(None,None),({"raw":supply_raw(usb="1")},{"raw":supply_raw(121,122)}),
                         ({"raw":supply_raw()},{"raw":supply_raw(119,122)}),
                         ({"raw":supply_raw()},{"raw":supply_raw(121,122,boot="22222222-2222-4222-8222-222222222222")})]:
            capture.update(supply_before=pre,supply_after=post)
            result,_=self.analyze(capture=capture,profile=profile)
            self.assertTrue(result["power"]["valid"])
            self.assertEqual(result["power"]["power_boundary"],"battery_net")
        capture["physical_serial"]="other-tablet"
        result,_=self.analyze(capture=capture)
        self.assertEqual(result["power"]["reason"],"source_profile_serial_mismatch")

    def test_complete_source_has_known_twenty_joules_and_sampled_memory(self):
        result, normalized = self.analyze()
        self.assertTrue(result["power"]["valid"])
        self.assertEqual(result["power"]["energy_j"], 20)
        self.assertEqual(result["power"]["power_boundary"], "battery_net")
        self.assertFalse(result["power"]["sensor_calibrated"])
        self.assertEqual(result["source_profile"], self.profile)
        self.assertEqual(result["memory"]["actual_sample_gap_max_s"], 5)
        self.assertEqual(result["memory"]["actual_sampled_window_start_s"], 110)
        self.assertIn(self.profile["units_validation_reference"], normalized["source_evidence"]["reference"])
        self.assertTrue(all(sample["external_online"] is None for sample in normalized["samples"]))

    def test_unknown_or_wrong_clock_rejects_memory_and_power(self):
        for clock in ("ts,clock_id,clock_name,clock_value\n", "ts,clock_id,clock_name,clock_value\n100,6,BOOTTIME,99\n"):
            with self.subTest(clock=clock):
                result, normalized = self.analyze(clock=clock)
                self.assertFalse(result["memory"]["valid"])
                self.assertEqual(result["memory"]["reason"], "memory_clock_unverified")
                self.assertFalse(result["power"]["valid"])
                self.assertIsNone(normalized)

    def test_memory_outside_capture_is_invalid_even_inside_requested_clip(self):
        extra = [self.point(99, 5, "MemTotal", "meminfo", "bytes", 1000),
                 self.point(99, 6, "MemAvailable", "meminfo", "bytes", 400)]
        result, _ = self.analyze(rows=self.rows + extra, start=111, end=119)
        self.assertFalse(result["memory"]["valid"])
        self.assertEqual(result["memory"]["reason"], "memory_outside_same_boot_capture_bounds")

    def test_profile_device_missing_units_and_references_cannot_be_manufactured(self):
        mutations = [("serial", "other-tablet"), ("serial", None), ("current_unit", None),
                     ("voltage_unit", "mV"), ("charge_unit", None),
                     ("units_validation_reference", ""), ("current_sign_reference", "  ")]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                profile = dict(self.profile, **{field: value})
                result, normalized = self.analyze(profile=profile)
                self.assertFalse(result["power"]["valid"])
                self.assertIsNone(normalized)

    def test_incomplete_or_cross_boot_capture_source_is_rejected(self):
        for mutate in (lambda item: item["after"].update(boot_id="22222222-2222-4222-8222-222222222222"),
                       lambda item: item["before"].pop("clock"),
                       lambda item: item["after"].update(t_s=float("nan")),
                       lambda item: item.update(serial=None)):
            capture = copy.deepcopy(self.capture)
            mutate(capture)
            with self.assertRaises(ValueError):
                self.analyze(capture=capture)

    def test_power_cannot_clip_missing_edges_memory_keeps_observed_subwindow(self):
        result, _ = self.analyze(start=100, end=200)
        self.assertFalse(result["power"]["valid"])
        self.assertEqual(result["power"]["reason"], "window_outside_sample_coverage")
        self.assertTrue(result["memory"]["valid"])
        self.assertEqual(result["memory"]["requested_window_start_s"], 100)
        self.assertEqual(result["memory"]["actual_sampled_window_start_s"], 110)
        self.assertFalse(result["memory"]["requested_edges_sampled"])


if __name__ == "__main__":
    unittest.main()
