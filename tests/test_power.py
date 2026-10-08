"""Positive controls and refusal cases for the physical measurement boundary."""

import unittest

from abbench.power import endpoint, integrate


def sample(time, current=-500_000, voltage=4_000_000, charge=None, online=False, boot="one"):
    return {
        "t_s": time, "boot_id": boot, "voltage_uv": voltage,
        "current_ua": current, "charge_uah": charge, "external_online": online,
    }


def config(**overrides):
    result = {
        "discharge_sign": -1, "power_boundary": "battery_net",
        "input_supply_verified_off": False, "counter_validated": True,
        "max_gap_s": 10,
    }
    result.update(overrides)
    return result


class IntegrationTests(unittest.TestCase):
    def test_read_latency_and_actual_cadence_are_reported_or_refused(self):
        rows = [dict(sample(0), read_end_s=0.1), dict(sample(2), read_end_s=2.2)]
        result = integrate(rows, config(max_read_span_s=0.25))
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["read_span_max_s"], 0.2)
        self.assertEqual(result["actual_sample_gap_mean_s"], 2)
        delayed = [dict(sample(0), read_end_s=0.9), dict(sample(2), read_end_s=2.9)]
        self.assertEqual(integrate(delayed, config(max_read_span_s=0.25))["reason"], "read_span_exceeded")
        self.assertEqual(integrate([sample(0), sample(2)], config(max_read_span_s=0.25))["reason"], "read_span_unknown")
        self.assertEqual(integrate(rows, config(max_read_span_s=-1))["reason"], "invalid_max_read_span_s")
        self.assertEqual(integrate([dict(sample(0), read_end_s=-1), sample(2)], config())["reason"], "invalid_read_end_s")

    def test_known_two_watts_for_ten_seconds(self):
        result = integrate([sample(0), sample(10)], config())
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["energy_j"], 20)
        self.assertAlmostEqual(result["energy_mwh"], 20 / 3.6)
        self.assertAlmostEqual(result["mean_w"], 2)
        self.assertEqual(result["power_boundary"], "battery_net")

    def test_charge_keeps_negative_sign_and_alternative_gauge_sign(self):
        charging = integrate([sample(0, 500_000), sample(10, 500_000)], config())
        self.assertTrue(charging["valid"])
        self.assertAlmostEqual(charging["energy_j"], -20)
        discharge = integrate([sample(0, 500_000), sample(10, 500_000)], config(discharge_sign=1))
        self.assertAlmostEqual(discharge["energy_j"], 20)

    def test_cli_null_windows_select_full_sample_coverage(self):
        result = integrate([sample(0), sample(10)],
                           config(window_start_s=None, window_end_s=None))
        self.assertTrue(result["valid"])
        self.assertEqual((result["window_start_s"], result["window_end_s"]), (0, 10))
        self.assertAlmostEqual(result["energy_j"], 20)
        self.assertFalse(result["window_clipped"])

    def test_supplied_tablet_net_current_cannot_be_whole_device(self):
        rows = [sample(0, -21_000, charge=8_000_000, online=True),
                sample(10, -21_000, charge=8_000_000, online=True)]
        net = integrate(rows, config())
        self.assertTrue(net["valid"])
        self.assertAlmostEqual(net["mean_w"], 0.084)
        side = integrate(rows, config(power_boundary="battery_side_device", input_supply_verified_off=True))
        self.assertFalse(side["valid"])
        self.assertEqual(side["reason"], "external_supply_online")
        self.assertIsNone(side["energy_j"])

    def test_whole_device_needs_verified_isolation_and_known_supply(self):
        boundary = config(power_boundary="battery_side_device")
        self.assertEqual(integrate([sample(0), sample(10)], boundary)["reason"],
                         "input_supply_not_verified_off")
        boundary["input_supply_verified_off"] = True
        self.assertTrue(integrate([sample(0), sample(10)], boundary)["valid"])
        self.assertEqual(integrate([sample(0, online=None), sample(10)], boundary)["reason"],
                         "external_supply_unknown")
        self.assertEqual(integrate([sample(0, 1), sample(10)], boundary)["reason"],
                         "charging_in_verified_battery_window")

    def test_broken_time_boot_gap_and_readings_are_refused(self):
        cases = [
            ([sample(0), sample(0)], config(), "non_increasing_time"),
            ([sample(1), sample(0)], config(), "non_increasing_time"),
            ([sample(0), sample(10, boot="two")], config(), "cross_boot_samples"),
            ([sample(0), sample(11)], config(), "sample_gap_exceeded"),
            ([sample(0), sample(10)], config(max_gap_s=None), "invalid_max_gap_s"),
            ([sample(0), sample(10, current=float("nan"))], config(), "invalid_current_ua"),
            ([sample(0), sample(10, voltage=float("inf"))], config(), "invalid_voltage_uv"),
        ]
        missing = sample(10)
        del missing["current_ua"]
        cases.append(([sample(0), missing], config(), "missing_sample_field"))
        for rows, settings, expected in cases:
            with self.subTest(expected=expected):
                result = integrate(rows, settings)
                self.assertFalse(result["valid"])
                self.assertEqual(result["reason"], expected)
                self.assertIsNone(result["mean_w"])

    def test_clip_uses_linear_power_and_never_extrapolates(self):
        # P rises from 0 W to 4 W in 10 s. Integral from 2 s to 8 s is 12 J.
        # V also changes, so separately interpolating V and I would give a
        # different answer and violate the declared linear POWER model.
        rows = [sample(0, current=0, voltage=2_000_000), sample(10, current=-1_000_000)]
        clipped = integrate(rows, config(window_start_s=2, window_end_s=8))
        self.assertTrue(clipped["valid"])
        self.assertAlmostEqual(clipped["energy_j"], 12)
        self.assertEqual(clipped["duration_s"], 6)
        self.assertEqual(clipped["interpolation"], "linear_power")
        self.assertTrue(clipped["boundary_interpolated"])
        self.assertIsNone(clipped["max_sampled_power_w"])
        self.assertEqual(integrate(rows, config(window_end_s=11))["reason"],
                         "window_outside_sample_coverage")

    def test_overflow_and_malformed_boundary_return_invalid_records(self):
        self.assertEqual(integrate([sample(0), sample(10)], config(power_boundary=[]))["reason"],
                         "invalid_power_boundary")
        self.assertEqual(integrate([sample(0), sample(10, voltage=10 ** 1000)], config())["reason"],
                         "invalid_voltage_uv")
        result = integrate([sample(0, voltage=1e308, current=-1e308),
                            sample(10, voltage=1e308, current=-1e308)], config())
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "nonfinite_calculated_power")


class EndpointTests(unittest.TestCase):
    def test_cli_null_windows_use_actual_endpoint_times(self):
        result = endpoint(sample(100, charge=100_000), sample(3700, charge=99_000),
                          config(window_start_s=None, window_end_s=None))
        self.assertTrue(result["valid"])
        self.assertEqual(result["duration_s"], 3600)
        self.assertAlmostEqual(result["energy_mwh"], 4)

    def test_known_charge_uses_actual_duration_and_endpoint_voltage(self):
        result = endpoint(sample(100, charge=8_000_000, voltage=4_100_000),
                          sample(3700, charge=7_999_000, voltage=3_900_000),
                          config(counter_resolution_uah=100))
        self.assertTrue(result["valid"])
        self.assertEqual(result["duration_s"], 3600)
        self.assertAlmostEqual(result["energy_mwh"], 4)
        self.assertAlmostEqual(result["mean_w"], 0.004)
        self.assertAlmostEqual(result["mean_current_ma"], 1)
        self.assertAlmostEqual(result["quantization_relative_bound"], 0.2)
        self.assertTrue(result["approximate"])

    def test_counter_validation_zero_and_coarse_quantization(self):
        first = sample(0, charge=100_000)
        for second, settings, expected in [
            (sample(10, charge=99_000), config(counter_validated=False), "counter_not_validated"),
            (sample(10, charge=100_000), config(), "below_counter_resolution"),
            (sample(10, charge=99_990), config(counter_resolution_uah=100), "below_counter_resolution"),
            (sample(10, charge=None), config(), "invalid_charge_uah"),
            (sample(10, charge=99_000, boot="two"), config(), "cross_boot_samples"),
        ]:
            with self.subTest(expected=expected):
                result = endpoint(first, second, settings)
                self.assertFalse(result["valid"])
                self.assertEqual(result["reason"], expected)
                self.assertIsNone(result["energy_j"])

    def test_charge_net_is_signed_but_endpoint_window_cannot_be_retimed(self):
        first, second = sample(0, charge=100_000), sample(10, charge=101_000)
        result = endpoint(first, second, config())
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["energy_j"], -14.4)
        self.assertFalse(result["resolution_known"])
        self.assertEqual(endpoint(first, second, config(window_end_s=9))["reason"],
                         "endpoint_window_mismatch")
        self.assertEqual(endpoint(first, second, config(window_start_s=False))["reason"],
                         "invalid_window")


if __name__ == "__main__":
    unittest.main()
