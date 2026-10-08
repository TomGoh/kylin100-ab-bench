import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.idle import parse_screen_state, parse_suspend_stats, run_idle


BOOT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class DeviceSimulation:
    def __init__(self, *, initial_screen="on", deep=False, failure=None,
                 reboot=False, constant_charge=False, missing_suspend=False,
                 ignored_write=None):
        self.now, self.screen = 10.0, initial_screen
        self.deep, self.failure, self.reboot = deep, failure, reboot
        self.constant_charge, self.missing_suspend = constant_charge, missing_suspend
        self.ignored_write = ignored_write
        self.settings = {"screen_off_timeout": "300000", "stay_on_while_plugged_in": "7"}
        self.original = dict(self.settings)
        self.calls, self.endpoint_count, self.suspend_count = [], 0, 0
        self.failed_once = False

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def run(self, args, *, capture_output, text, timeout):
        command = shlex.split(args[-1])[0]
        self.calls.append((self.now, command))
        self.now += 0.1
        if self.failure and self.failure in command and not self.failed_once:
            self.failed_once = True
            # Simulate a write that landed before its transport timed out.
            if "settings put" in command:
                tokens = command.split()
                self.settings[tokens[-2]] = tokens[-1]
            raise subprocess.TimeoutExpired(args, timeout, output=b"partial", stderr=b"USB timeout")
        boot = OTHER if self.reboot and self.endpoint_count >= 2 else BOOT
        if command == "cat /proc/sys/kernel/random/boot_id":
            raw = boot + "\n"
        elif "boot_before=" in command:
            self.endpoint_count += 1
            boot = OTHER if self.reboot and self.endpoint_count >= 2 else BOOT
            charge = 1000 if self.constant_charge or self.endpoint_count == 1 else 990
            raw = (f"boot_before={boot}\nread_begin_s={self.now + 100}\n"
                   f"voltage_candidate=4000000\ncharge_candidate={charge}\n"
                   f"physical_usb_online=1\nread_end_s={self.now + 100.2}\nboot_after={boot}\n")
        elif command == "dumpsys power":
            raw = "  mWakefulness=" + ("Awake" if self.screen == "on" else "Asleep") + "\nDisplay Power: object@123\n"
        elif command == "dumpsys display":
            state = self.screen.upper()
            raw = f'DisplayDeviceInfo{{"内置屏幕": type INTERNAL, state {state}, committedState {state}, FLAG_ALLOWED_TO_BE_DEFAULT_DISPLAY}}\n'
        elif command == "dumpsys suspend_control_internal":
            self.suspend_count += 1
            if self.missing_suspend:
                return subprocess.CompletedProcess(args, 1, "", "service not found")
            count = 1 if self.deep and self.suspend_count > 1 else 0
            raw = f"success: {count}\ntotal suspend time: {5000 * count} ms\n"
        elif "settings get" in command:
            raw = self.settings[command.split()[-1]] + "\n"
        elif "settings put" in command:
            tokens = command.split()
            if tokens[-2] != self.ignored_write:
                self.settings[tokens[-2]] = tokens[-1]
            raw = ""
        elif "settings delete" in command:
            self.settings[command.split()[-1]] = "null"
            raw = ""
        elif "input keyevent" in command:
            key = command.split()[-1]
            if key in ("224", "223"):
                self.screen = "on" if key == "224" else "off"
            raw = ""
        else:
            raise AssertionError("unexpected device command: " + command)
        return subprocess.CompletedProcess(args, 0, raw, "")


class IdleWindowTests(unittest.TestCase):
    def collect(self, simulation, supply_observations=None, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "window"
            with patch("abbench.idle.time.monotonic", simulation.monotonic), \
                 patch("abbench.idle.time.sleep", simulation.sleep), \
                 patch("abbench.idle.subprocess.run", simulation.run), \
                 patch("abbench.idle._mark_event", return_value={"t_s": 123, "boot_id": BOOT}), \
                 patch("abbench.idle._read_supply", side_effect=supply_observations,
                       return_value={"valid": False, "reason": "not_observed"}), \
                 patch("abbench.idle._start_capture", return_value={"state": "active"}) as start, \
                 patch("abbench.idle._stop_capture", return_value={"state": "exported"}) as stop:
                result = run_idle("test-device", directory, duration_s=10, **kwargs)
            self.assertEqual(json.loads((directory / "idle.json").read_text()), result)
            events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
            return result, events, start, stop

    def test_screen_on_uses_one_capture_and_restores_original_settings_and_screen(self):
        simulation = DeviceSimulation(initial_screen="off")
        result, _, start, stop = self.collect(simulation)
        self.assertTrue(result["valid"])
        self.assertEqual(result["kind_observed"], "screen_on_idle")
        start.assert_called_once()
        stop.assert_called_once()
        self.assertEqual(result["capture"]["state"], "exported")
        self.assertEqual(simulation.settings, simulation.original)
        self.assertEqual(simulation.screen, "off")
        self.assertTrue(result["settings_restored"])
        self.assertFalse(result["power"]["valid"])
        self.assertIsNone(result["power"]["mean_w"])

    def test_standby_has_no_device_queries_or_capture_inside_host_wait(self):
        simulation = DeviceSimulation(deep=True)
        result, events, start, stop = self.collect(simulation, kind="screen_off_standby")
        self.assertTrue(result["valid"])
        self.assertEqual(result["kind_observed"], "screen_off_standby")
        start.assert_not_called()
        stop.assert_not_called()
        first = next(item["host_s"] for item in events if item["event"] == "window-start")
        last = next(item["host_s"] for item in events if item["event"] == "window-end")
        self.assertFalse(any(first <= stamp < last for stamp, _ in simulation.calls))
        self.assertAlmostEqual(last - first, 10)
        self.assertGreater(result["elapsed_boottime_s"], 10)
        self.assertTrue(result["wake_overhead"]["included_in_endpoint_window"])
        self.assertEqual(result["start"]["physical_usb_online"], True)
        self.assertNotIn("external_online", result["start"])

    def test_zero_or_missing_suspend_evidence_is_screen_off_idle(self):
        for simulation in (DeviceSimulation(), DeviceSimulation(missing_suspend=True)):
            with self.subTest(missing=simulation.missing_suspend):
                result, _, _, _ = self.collect(simulation, kind="screen_off_standby")
                self.assertTrue(result["valid"])
                self.assertEqual(result["kind_observed"], "screen_off_idle")
                self.assertFalse(result["power"]["valid"])

    def test_validated_charge_endpoint_has_known_signed_energy(self):
        config = {"units_validated": True, "counter_validated": True,
                  "power_boundary": "battery_net", "counter_resolution_uah": 1}
        result, _, _, _ = self.collect(DeviceSimulation(), kind="screen_off_standby", power_config=config)
        self.assertTrue(result["power"]["valid"])
        self.assertAlmostEqual(result["power"]["energy_j"], 0.144)
        self.assertEqual(result["power"]["power_boundary"], "battery_net")
        self.assertAlmostEqual(result["power"]["mean_w"], 0.144 / result["elapsed_boottime_s"])

    def test_counter_not_validated_or_constant_never_reports_zero_power(self):
        configs = [{"counter_validated": True, "power_boundary": "battery_net"},
                   {"units_validated": True, "counter_validated": True, "power_boundary": "battery_net"}]
        for config in configs:
            result, _, _, _ = self.collect(DeviceSimulation(constant_charge=True), power_config=config)
            self.assertTrue(result["valid"])
            self.assertFalse(result["power"]["valid"])
            self.assertIsNone(result["power"]["mean_w"])

    def test_raw_usb_flag_cannot_authorize_whole_device_power(self):
        config = {"units_validated": True, "counter_validated": True,
                  "power_boundary": "battery_side_device", "input_supply_verified_off": True}
        result, _, _, _ = self.collect(DeviceSimulation(), power_config=config)
        self.assertFalse(result["power"]["valid"])
        self.assertEqual(result["power"]["reason"], "external_supply_unknown")

    def test_counter_endpoints_require_actual_bracketing_supply_evidence(self):
        def supply(t, usb):
            return {"raw": f"ABSUPPLY\t1\nBEGIN\t{BOOT}\ttest-device\t{t}\n"
                    "NODE\t/sys/class/power_supply/battery\tBattery\t1\t1\n"
                    f"NODE\t/sys/class/power_supply/usb\tUSB\t{usb}\t?\n"
                    "DUMP_BEGIN\nCurrent Battery Service state:\n"
                    "  AC powered: false\n  USB powered: false\n"
                    "  Wireless powered: false\n  Dock powered: false\nDUMP_END\n"
                    f"END\t{BOOT}\ttest-device\t{t+0.1}\n"}
        config = {"serial": "test-device", "units_validated": True, "counter_validated": True,
                  "counter_resolution_uah": 1, "power_boundary": "battery_side_device"}
        for usb, expected in ((0, True), (1, False)):
            with self.subTest(usb=usb):
                result, _, _, _ = self.collect(DeviceSimulation(), kind="screen_off_standby", power_config=config,
                                               supply_observations=[supply(90, 0), supply(300, usb)])
                self.assertEqual(result["supply_evidence"]["verified_off"], expected)
                self.assertEqual(result["power"]["valid"], expected)
                if expected:
                    self.assertEqual(result["power"]["power_boundary"], "battery_side_device")
                    self.assertAlmostEqual(result["power"]["energy_j"], 0.144)

    def test_static_external_off_flags_cannot_bypass_missing_evidence(self):
        config = {"serial": "test-device", "units_validated": True, "counter_validated": True,
                  "power_boundary": "battery_side_device", "input_supply_verified_off": True,
                  "external_online_verified": False, "counter_resolution_uah": 1}
        result, _, _, _ = self.collect(DeviceSimulation(), kind="screen_off_standby", power_config=config)
        self.assertFalse(result["power"]["valid"])
        self.assertEqual(result["power"]["reason"], "external_supply_unknown")

    def test_transport_timeout_after_a_setting_write_restores_the_landed_write(self):
        simulation = DeviceSimulation(failure="settings put system screen_off_timeout")
        result, events, _, _ = self.collect(simulation)
        self.assertFalse(result["valid"])
        self.assertIn("device_command_failed", result["reason"])
        self.assertEqual(simulation.settings, simulation.original)
        self.assertTrue(result["settings_restored"])
        self.assertTrue(any(item.get("timed_out") for item in events))

    def test_ignored_successful_setting_write_is_rejected_and_restored(self):
        for key in ("screen_off_timeout", "stay_on_while_plugged_in"):
            with self.subTest(key=key):
                simulation = DeviceSimulation(ignored_write=key)
                result, _, start, stop = self.collect(simulation)
                self.assertFalse(result["valid"])
                self.assertEqual(result["reason"], "screen_setting_readback_mismatch_" + key)
                self.assertFalse(result["settings"][key]["applied"])
                self.assertEqual(result["settings"][key]["observed"], simulation.original[key])
                self.assertEqual(simulation.settings, simulation.original)
                self.assertTrue(result["settings_restored"])
                self.assertTrue(result["restore"][key]["restored"])
                start.assert_not_called()
                stop.assert_not_called()

    def test_failure_after_sleep_restores_screen_without_toggle(self):
        simulation = DeviceSimulation(failure="voltage_candidate=")
        result, _, start, _ = self.collect(simulation, kind="screen_off_standby")
        self.assertFalse(result["valid"])
        self.assertTrue(result["settings_restored"])
        self.assertEqual(simulation.screen, "on")
        start.assert_not_called()
        self.assertFalse(any(command == "input keyevent 26" for _, command in simulation.calls))

    def test_cross_boot_skips_restoration_in_new_session_and_invalidates_window(self):
        simulation = DeviceSimulation(reboot=True)
        result, _, _, _ = self.collect(simulation, kind="screen_off_standby")
        self.assertFalse(result["valid"])
        self.assertIn("device_restarted_during_window", result["reason"])
        self.assertFalse(result["settings_restored"])
        self.assertTrue(all(item["reason"] == "original_boot_not_verified" for item in result["restore"].values()))
        self.assertFalse(any("exit 41" in command for _, command in simulation.calls))

    def test_missing_original_setting_does_not_invent_a_restore_value(self):
        simulation = DeviceSimulation()
        simulation.settings["screen_off_timeout"] = "null"
        result, _, _, _ = self.collect(simulation)
        self.assertTrue(result["valid"])
        self.assertEqual(simulation.settings["screen_off_timeout"], "null")
        self.assertTrue(any("settings delete system screen_off_timeout" in command for _, command in simulation.calls))

    def test_screen_parser_requires_matching_physical_and_global_evidence(self):
        actual_format = 'DisplayDeviceInfo{"内置屏幕": state ON, committedState ON, FLAG_ALLOWED_TO_BE_DEFAULT_DISPLAY}'
        self.assertEqual(parse_screen_state(" mWakefulness=Awake\nDisplay Power: object@123\n", actual_format), "on")
        self.assertIsNone(parse_screen_state("mWakefulness=Awake\nDisplay Power: object@123"))
        self.assertIsNone(parse_screen_state("mWakefulness=Asleep", actual_format))
        self.assertIsNone(parse_screen_state("mWakefulness=Awake", actual_format.replace("committedState ON", "committedState OFF")))
        self.assertIsNone(parse_screen_state("mWakefulness=Awake", actual_format + "\n" + actual_format))
        off = actual_format.replace('state ON', 'state OFF').replace('committedState ON', 'committedState OFF')
        self.assertEqual(parse_screen_state('mWakefulness=Dozing', off), 'off')
        self.assertIsNone(parse_screen_state('mWakefulness=Dozing', off.replace('state OFF', 'state DOZE').replace('committedState OFF', 'committedState DOZE')))
        self.assertEqual(parse_suspend_stats("success: 3\ntotal suspend time: 42 ms\n"),
                         {"success": 3, "total_suspend_ms": 42})
        self.assertIsNone(parse_suspend_stats("total suspend time: 3 s")["total_suspend_ms"])

    def test_invalid_duration_is_rejected_before_device_operations(self):
        with tempfile.TemporaryDirectory() as temporary, patch("abbench.idle.subprocess.run") as run:
            for duration in (None, 0, True, 1801, 3.1):
                with self.subTest(duration=duration), self.assertRaises(ValueError):
                    run_idle("device", Path(temporary) / "unused", duration_s=duration)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
