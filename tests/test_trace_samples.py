"""Check exact batching and provenance before reusing a power integrator."""

import csv
import hashlib
import tempfile
import unittest
from pathlib import Path

from abbench.power import integrate
from abbench.trace_samples import normalize_battery_csv


COLUMNS = ["ts_ns", "track_id", "name", "type", "unit", "source_arg_set_id",
           "dimension_arg_set_id", "value"]
UNITS = {"batt.current_ua": "uA", "batt.voltage_uv": "uV", "batt.charge_uah": "uAh"}
BOOT = "test-boot-one"


def row(time, track, name, value, **changes):
    result = {
        "ts_ns": str(time), "track_id": str(track), "name": name,
        "type": "battery_counter", "unit": "[NULL]", "source_arg_set_id": "[NULL]",
        "dimension_arg_set_id": str(track), "value": str(value),
    }
    result.update(changes)
    return result


def records():
    result = []
    for time in (0, 10_000_000_000):
        result.extend([
            row(time, 0, "batt.charge_uah", 100_000),
            row(time, 2, "batt.current_ua", -500_000),
            row(time, 4, "batt.voltage_uv", 4_000_000),
        ])
    return result


class TraceSamplesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "raw.csv"

    def write(self, rows, columns=None):
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns or COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return self.path

    def evidence(self, **changes):
        evidence = {
            "boot_id": BOOT, "csv_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            "clock": "CLOCK_BOOTTIME", "ts_unit": "ns", "units": dict(UNITS),
            "reference": "known fixture units and boot-clock binding",
        }
        evidence.update(changes)
        return evidence

    def normalize(self, rows=None, *, evidence_changes=None, **arguments):
        if rows is not None:
            self.write(rows)
        settings = {"boot_id": BOOT, "source_verified": True,
                    "source_evidence": self.evidence(**(evidence_changes or {}))}
        settings.update(arguments)
        return normalize_battery_csv(self.path, **settings)

    def test_known_two_watts_retains_provenance_without_measurement_claim(self):
        converted = self.normalize(records())
        self.assertTrue(converted["valid"])
        self.assertEqual(converted["sample_count"], 2)
        self.assertEqual(converted["raw_track_count"], 3)
        self.assertFalse(converted["measurement_validated"])
        self.assertFalse(converted["counter_validated"])
        self.assertFalse(converted["input_supply_verified_off"])
        sample = converted["samples"][0]
        self.assertEqual(sample["current_ua"], -500_000)
        self.assertIsNone(sample["external_online"])
        self.assertEqual(sample["source_track_ids"]["voltage_uv"], 4)
        self.assertEqual(sample["declared_units"]["voltage_uv"], "uV")
        self.assertEqual(sample["source_csv_sha256"], converted["source_csv"]["sha256"])
        energy = integrate(converted["samples"], {
            "discharge_sign": -1, "power_boundary": "battery_net", "max_gap_s": 10,
        })
        self.assertTrue(energy["valid"])
        self.assertAlmostEqual(energy["energy_j"], 20)
        self.assertAlmostEqual(energy["mean_w"], 2)
        refused = integrate(converted["samples"], {
            "discharge_sign": -1, "power_boundary": "battery_side_device",
            "input_supply_verified_off": True, "max_gap_s": 10,
        })
        self.assertFalse(refused["valid"])
        self.assertEqual(refused["reason"], "external_supply_unknown")

    def test_source_requires_explicit_unit_clock_and_file_boot_binding(self):
        self.write(records())
        for changes, arguments, reason in [
            ({}, {"source_verified": False}, "source_units_and_clock_not_verified"),
            ({}, {"source_evidence": None}, "missing_source_evidence"),
            ({"units": {"batt.current_ua": "mA"}}, {}, "unconfirmed_battery_units"),
            ({"clock": "CLOCK_MONOTONIC"}, {}, "unsupported_or_unverified_clock"),
            ({"ts_unit": "s"}, {}, "unsupported_or_unverified_clock"),
            ({"boot_id": "different-boot"}, {}, "source_boot_binding_mismatch"),
            ({"csv_sha256": "0" * 64}, {}, "source_csv_binding_mismatch"),
            ({"reference": ""}, {}, "missing_evidence_reference"),
        ]:
            with self.subTest(reason=reason):
                result = self.normalize(evidence_changes=changes, **arguments)
                self.assertFalse(result["valid"])
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["samples"], [])

    def test_supply_state_requires_full_window_evidence(self):
        self.write(records())
        self.assertFalse(self.normalize(external_online=False)["valid"])
        supply = {
            "boot_id": BOOT, "csv_sha256": self.evidence()["csv_sha256"],
            "reference": "verified actual input supply state for the complete window",
            "external_online": True, "state_verified": True, "covers_entire_window": True,
        }
        known = self.normalize(external_online=True, supply_evidence=supply)
        self.assertTrue(known["valid"])
        self.assertTrue(all(sample["external_online"] is True for sample in known["samples"]))
        self.assertFalse(known["input_supply_verified_off"])
        supply["covers_entire_window"] = False
        self.assertEqual(self.normalize(external_online=True, supply_evidence=supply)["reason"],
                         "external_supply_state_not_verified_for_window")

    def test_missing_voltage_and_nonmatching_batches_are_rejected(self):
        missing = [item for item in records() if item["name"] != "batt.voltage_uv"]
        self.assertEqual(self.normalize(missing)["reason"], "missing_required_battery_track")
        staggered = records()
        staggered[2]["ts_ns"] = "1"
        self.assertEqual(self.normalize(staggered)["reason"], "unsynchronized_battery_timestamps")
        incomplete = records()[:-1]
        self.assertEqual(self.normalize(incomplete)["reason"], "unsynchronized_battery_timestamps")

    def test_missing_charge_is_null_and_unrelated_counters_are_not_used(self):
        rows = [item for item in records() if item["name"] != "batt.charge_uah"]
        rows.insert(2, row(5_000_000_000, 8, "batt.power_mw", 999999))
        result = self.normalize(rows)
        self.assertTrue(result["valid"])
        self.assertEqual(result["raw_track_count"], 3)
        self.assertEqual(result["selected_track_count"], 2)
        self.assertTrue(all(sample["charge_uah"] is None for sample in result["samples"]))
        self.assertNotIn("batt.power_mw", result["selected_track_ids"])

    def test_bad_numbers_voltage_units_and_type_are_rejected(self):
        for index, field, value, expected in [
            (1, "value", "", "invalid_counter_number"),
            (1, "value", "NaN", "nonfinite_counter_value"),
            (1, "value", "Inf", "nonfinite_counter_value"),
            (2, "value", "0", "invalid_voltage_uv"),
            (2, "value", "-1", "invalid_voltage_uv"),
            (0, "value", "-1", "invalid_charge_uah"),
            (2, "unit", "mV", "csv_unit_conflicts_with_verified_binding"),
            (1, "type", "fake_powerstats", "unexpected_battery_track_type"),
            (1, "ts_ns", "0.5", "invalid_counter_number"),
        ]:
            with self.subTest(value=value, expected=expected):
                rows = records()
                rows[index][field] = value
                result = self.normalize(rows)
                self.assertFalse(result["valid"])
                self.assertEqual(result["reason"], expected)

    def test_duplicates_reversed_time_and_track_aliases_are_rejected(self):
        duplicate = records()
        duplicate.insert(2, dict(duplicate[1]))
        self.assertEqual(self.normalize(duplicate)["reason"], "duplicate_counter_point")
        backwards = records()[3:] + records()[:3]
        self.assertEqual(self.normalize(backwards)["reason"], "counter_timestamps_out_of_order")
        alias = records()
        alias[4]["track_id"] = "20"
        self.assertEqual(self.normalize(alias)["reason"], "multiple_tracks_for_battery_field")
        reused = records()
        reused.insert(3, row(5_000_000_000, 2, "MemTotal", 123, type="meminfo", unit="bytes"))
        self.assertEqual(self.normalize(reused)["reason"], "track_id_identity_conflict")

    def test_embedded_boot_binding_cannot_cross_boots(self):
        rows = records()
        for item in rows:
            item["boot_id"] = BOOT
        rows[-1]["boot_id"] = "second-boot"
        self.write(rows, COLUMNS + ["boot_id"])
        self.assertEqual(self.normalize()["reason"], "row_boot_binding_mismatch")

    def test_coarse_float_seconds_do_not_collapse_distinct_raw_timestamps(self):
        rows = records()
        for index, item in enumerate(rows):
            item["ts_ns"] = str(9_000_000_000_000_000_000 + (index // 3))
        self.assertEqual(self.normalize(rows)["reason"], "timestamp_precision_loss")


if __name__ == "__main__":
    unittest.main()
