import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.environment import capture_environment, capture_memory_baseline, parse_battery, thermal_gate


BOOT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class Simulation:
    def __init__(self, *, cross_at=None, missing_temperature=False, missing_mem=False,
                 missing_zram=False, timeout_command=None, delay=0.1):
        self.now, self.calls, self.sleeps = 10.0, [], []
        self.cross_at, self.delay = cross_at, delay
        self.missing_temperature, self.missing_mem = missing_temperature, missing_mem
        self.missing_zram, self.timeout_command = missing_zram, timeout_command
        self.memory_count = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, args, *, capture_output, text, timeout):
        script = shlex.split(args[-1])[0]
        command = script.split("AB_ENV_PAYLOAD\\n'; ( ", 1)[1].split(" ); rc=$?", 1)[0]
        self.calls.append((command, timeout))
        before = self.now + 100
        if self.timeout_command and self.timeout_command in command:
            self.now += timeout
            raise subprocess.TimeoutExpired(args, timeout, output=b"partial telemetry", stderr=b"timed out")
        if self.delay > timeout:
            self.now += timeout
            raise subprocess.TimeoutExpired(args, timeout, output=b"partial telemetry")
        self.now += self.delay
        after = self.now + 100
        boot = OTHER if self.cross_at is not None and len(self.calls) >= self.cross_at else BOOT
        rc = 0
        if command == "true":
            payload = ""
        elif command == "getprop ro.build.fingerprint":
            payload = "device/test/fingerprint\n"
        elif command.startswith("sha256sum"):
            payload = "a" * 64 + "  /sys/kernel/notes\nLinux test-device 5.15\n"
        elif command.startswith("dumpsys battery"):
            payload = "level: 60\nstatus: 3\n" + ("" if self.missing_temperature else "temperature: 350\n")
            payload += "\nAB_CURRENT_CANDIDATE\n-10000\n"
        elif command.startswith("printf 'screen_brightness="):
            payload = "screen_brightness=96\nscreen_brightness_mode=0\npeak_refresh_rate=90.0\nvolume_music=3\nmin_refresh_rate=null\n"
        elif command == "cat /proc/meminfo":
            self.memory_count += 1
            payload = "MemTotal: 1000 kB\n" + ("" if self.missing_mem else f"MemAvailable: {400 - self.memory_count} kB\n")
        elif command.startswith("cat /sys/block/zram0/mm_stat"):
            payload, rc = ("", 1) if self.missing_zram else ("307200 102400 131072 0 0 1\n", 0)
        elif command == "cat /proc/swaps":
            payload = "Filename Type Size Used Priority\n"
        elif command == "dumpsys meminfo":
            payload = "Total PSS by process:\n"
        else:
            payload = "raw platform value 1234\n"
        raw = (f"AB_ENV_BOOT_BEFORE\n{boot}\nAB_ENV_TIME_BEFORE\n{before} 22.0\n"
               f"AB_ENV_PAYLOAD\n{payload}\nAB_ENV_RC={rc}\nAB_ENV_TIME_AFTER\n{after} 22.0\n"
               f"AB_ENV_BOOT_AFTER\n{boot}\n")
        return subprocess.CompletedProcess(args, 0, raw, "unavailable" if rc else "")


class EnvironmentTests(unittest.TestCase):
    def collect(self, simulation, function=capture_environment, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "snapshot"
            with patch("abbench.environment.time.monotonic", simulation.monotonic), \
                 patch("abbench.environment.time.sleep", simulation.sleep), \
                 patch("abbench.environment.subprocess.run", simulation.run):
                result = function("test-device", out, **kwargs)
            filename = "environment.json" if function == capture_environment else "memory-baseline.json"
            self.assertEqual(json.loads((out / filename).read_text()), result)
            raw = [path.read_text() for path in out.glob("*.txt")]
            return result, raw

    def test_known_environment_preserves_identity_settings_and_raw_thermal(self):
        simulation = Simulation()
        result, raw = self.collect(simulation, expected_boot_id=BOOT)
        self.assertTrue(result["valid"])
        self.assertEqual(result["boot_id"], BOOT)
        self.assertEqual(result["fingerprint"], "device/test/fingerprint")
        self.assertEqual(result["kernel_runtime_sha256"], "a" * 64)
        self.assertEqual(result["battery_temperature_c"], 35)
        self.assertEqual(result["battery"]["current_ua_candidate"], -10000)
        self.assertEqual(result["settings"]["screen_brightness"], "96")
        self.assertEqual(result["settings"]["music_volume"], "3")
        self.assertIsNone(result["settings"]["min_refresh_rate"])
        self.assertFalse(result["thermal_units_verified"])
        self.assertTrue(any("raw platform value 1234" in item for item in raw))
        self.assertFalse(any(command == "dumpsys meminfo" for command, _ in simulation.calls))
        self.assertTrue(all(timeout <= 10 for _, timeout in simulation.calls))

    def test_cross_boot_environment_or_wrong_expected_boot_is_invalid(self):
        for simulation, expected in ((Simulation(cross_at=4), BOOT), (Simulation(), OTHER)):
            with self.subTest(expected=expected):
                result, _ = self.collect(simulation, expected_boot_id=expected)
                self.assertFalse(result["valid"])
                self.assertIn("cross_boot_capture", result["reason"])

    def test_missing_temperature_does_not_become_zero_or_pass_formal_gate(self):
        result, _ = self.collect(Simulation(missing_temperature=True))
        self.assertTrue(result["valid"])
        self.assertIsNone(result["battery_temperature_c"])
        gate = thermal_gate(result)
        self.assertFalse(gate["valid"])
        self.assertEqual(gate["reason"], "battery_temperature_unavailable")

    def test_live_battery_gate_limits_absolute_and_relative_temperature(self):
        environment = {"valid": True, "battery_temperature_c": 35,
                       "battery": {"temperature_is_live": True}}
        self.assertTrue(thermal_gate(environment, baseline_temperature_c=34)["valid"])
        self.assertFalse(thermal_gate(environment, baseline_temperature_c=33)["valid"])
        environment["battery_temperature_c"] = 40.1
        self.assertEqual(thermal_gate(environment)["reason"], "battery_temperature_above_start_limit")
        environment["battery"]["temperature_is_live"] = False
        self.assertEqual(thermal_gate(environment)["reason"], "battery_temperature_not_confirmed_live")
        environment["valid"] = False
        self.assertEqual(thermal_gate(environment)["reason"], "environment_capture_invalid")
        self.assertFalse(parse_battery("UPDATES STOPPED -- use reset\ntemperature: 350")["temperature_is_live"])
        with self.assertRaises(ValueError):
            thermal_gate({"valid": True, "battery_temperature_c": 30, "battery": {"temperature_is_live": True}}, baseline_temperature_c=float("nan"))

    def test_three_memory_snapshots_are_one_boot_baseline_with_known_bytes(self):
        simulation = Simulation()
        result, _ = self.collect(simulation, capture_memory_baseline)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["samples"]), 3)
        self.assertEqual(result["aggregate"]["independent_sample_count"], 1)
        self.assertFalse(result["aggregate"]["independent"])
        self.assertEqual(result["aggregate"]["mean_mem_available_bytes"], 398 * 1024)
        self.assertEqual(result["aggregate"]["min_mem_available_bytes"], 397 * 1024)
        self.assertEqual(result["aggregate"]["mean_estimated_unavailable_bytes"], 602 * 1024)
        self.assertEqual(simulation.sleeps, [10, 10])
        self.assertIsNone(result["process_meminfo"])

    def test_missing_required_memory_or_cross_boot_never_creates_zero_aggregate(self):
        for simulation in (Simulation(missing_mem=True), Simulation(cross_at=4)):
            result, _ = self.collect(simulation, capture_memory_baseline)
            self.assertFalse(result["valid"])
            self.assertIsNone(result["aggregate"])

    def test_missing_zram_remains_optional_null_and_process_meminfo_is_single_opt_in(self):
        simulation = Simulation(missing_zram=True)
        result, _ = self.collect(simulation, capture_memory_baseline, include_process_meminfo=True)
        self.assertTrue(result["valid"])
        self.assertTrue(all(sample["summary"]["zram"] is None for sample in result["samples"]))
        self.assertEqual(sum(command == "dumpsys meminfo" for command, _ in simulation.calls), 1)
        self.assertTrue(result["process_meminfo"]["outside_power_window_required"])

    def test_deadline_clamps_command_and_memory_sleep_and_marks_late_invalid(self):
        simulation = Simulation(delay=10)
        result, raw = self.collect(simulation, deadline_monotonic_s=11)
        self.assertFalse(result["valid"])
        self.assertIn("deadline_exhausted", result["reason"])
        self.assertAlmostEqual(simulation.now, 11)
        self.assertEqual(simulation.calls[0][1], 1)
        self.assertTrue(any("partial telemetry" in item for item in raw))
        simulation = Simulation()
        result, _ = self.collect(simulation, capture_memory_baseline, interval_s=90, deadline_monotonic_s=12)
        self.assertFalse(result["valid"])
        self.assertAlmostEqual(simulation.now, 12)
        self.assertTrue(all(chunk <= 30 for chunk in simulation.sleeps))
        self.assertEqual(len(result["samples"]), 1)

    def test_optional_timeout_preserves_failure_without_inventing_thermal_values(self):
        result, raw = self.collect(Simulation(timeout_command="thermal_zone"))
        self.assertTrue(result["valid"])
        self.assertFalse(result["commands"]["thermal"]["available"])
        self.assertEqual(result["commands"]["thermal"]["error"], "command_timeout")
        self.assertTrue(any("partial telemetry" in item for item in raw))

    def test_invalid_controls_fail_before_device_calls(self):
        with tempfile.TemporaryDirectory() as temporary, patch("abbench.environment.subprocess.run") as run:
            for kwargs in ({"count": 0}, {"count": True}, {"count": 11},
                           {"interval_s": float("inf")}, {"include_process_meminfo": "yes"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    capture_memory_baseline("device", Path(temporary) / "unused", **kwargs)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
