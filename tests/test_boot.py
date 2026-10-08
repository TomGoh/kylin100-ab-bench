import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.boot import measure_boot, parse_desktop_evidence, parse_probe


OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-8333-333333333333"


def probe(boot_id=OLD, complete="1", uptime=100, after=None):
    return (f"boot_id_before={boot_id}\nboot_completed={complete}\n"
            f"uptime={uptime} 37.00\nboot_id_after={after or boot_id}\n")


def desktop_evidence(*, unlocked="true", boot=NEW, resumed="com.example.launcher",
                     home="com.example.launcher/.Launcher", animation="stopped", exit_value="1"):
    return (f"AB_DESKTOP_BOOT_BEFORE\n{boot}\nAB_DESKTOP_HOME\n{home}\n"
            f"AB_DESKTOP_UNLOCKED\n{unlocked}\nAB_DESKTOP_BOOTANIM\n{animation}\n"
            f"AB_DESKTOP_BOOTANIM_EXIT\n{exit_value}\nAB_DESKTOP_ACTIVITY\n"
            f"  mResumedActivity: ActivityRecord{{abcd u0 {resumed}/.Activity t1}}\n"
            f"AB_DESKTOP_BOOT_AFTER\n{boot}\n")


class Simulation:
    """Model ADB output and real elapsed time, including timeout consumption."""
    def __init__(self, probes, *, reboot_error=False, diagnostics_fail=False):
        self.now = 10.0
        self.probes = list(probes)
        self.calls = []
        self.reboot_error = reboot_error
        self.diagnostics_fail = diagnostics_fail

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def run(self, args, *, capture_output, text, timeout):
        self.calls.append({"args": args, "start": self.now, "timeout": timeout})
        delay = 0.2
        if args[-1] == "reboot":
            value = ""
            failed = self.reboot_error
        elif "boot_id_before=" in args[-1]:
            value = self.probes.pop(0) if self.probes else probe()
            failed = False
            if isinstance(value, tuple):
                delay, value = value
        else:
            value = "absolute_boot_time 27\nboot_complete 27074\n"
            failed = self.diagnostics_fail
        if delay > timeout:
            self.now += timeout
            raise subprocess.TimeoutExpired(args, timeout, output=b"partial read", stderr=b"late")
        self.now += delay
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(args, int(failed), value, "permission denied" if failed else "")


class DesktopSimulation(Simulation):
    def __init__(self, probes, desktop_outputs):
        super().__init__(probes)
        self.desktop_outputs = list(desktop_outputs)

    def run(self, args, **kwargs):
        if "AB_DESKTOP_BOOT_BEFORE" in args[-1]:
            self.calls.append({"args": args, "start": self.now, "timeout": kwargs["timeout"]})
            self.now += min(0.2, kwargs["timeout"])
            return subprocess.CompletedProcess(args, 0, self.desktop_outputs.pop(0), "")
        return super().run(args, **kwargs)


class BootObserverTests(unittest.TestCase):
    def collect(self, simulation, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "new-run"
            with patch("abbench.boot.time.monotonic", simulation.monotonic), \
                 patch("abbench.boot.time.sleep", simulation.sleep), \
                 patch("abbench.boot.subprocess.run", simulation.run):
                result = measure_boot("test-device", directory, **kwargs)
            saved = json.loads((directory / "boot.json").read_text())
            self.assertEqual(result, saved)
            files = {path.name: path.read_text() for path in directory.iterdir()}
            return result, files

    def test_new_boot_completion_has_conservative_request_interval(self):
        simulation = Simulation([probe(), probe(NEW, "0", 2), probe(NEW, "1", 4),
                                 probe(NEW, "1", 5)])
        result, files = self.collect(simulation, reboot=True, timeout_s=15)
        self.assertTrue(result["valid"])
        self.assertEqual((result["old_boot_id"], result["new_boot_id"]), (OLD, NEW))
        self.assertEqual(result["boot_id"], NEW)
        self.assertAlmostEqual(result["request_to_system_complete_interval_s"]["lower"], 0.2)
        self.assertAlmostEqual(result["request_to_system_complete_interval_s"]["upper"], 1.6)
        self.assertAlmostEqual(result["request_to_system_complete_observed_s"], 1.6)
        self.assertIsNone(result["elapsed_s"])
        self.assertEqual(result["desktop_ready"]["reason"], "device_adapter_not_implemented")
        self.assertEqual(sum(call["args"][-1] == "reboot" for call in simulation.calls), 1)
        self.assertTrue(any("absolute_boot_time 27\nboot_complete 27074" in text for text in files.values()))
        self.assertIn("probe-interpretation", files["observations.jsonl"])

    def test_default_external_reboot_never_sends_reboot_and_trigger_stays_unknown(self):
        result, _ = self.collect(Simulation([probe(), probe(NEW, "0"), probe(NEW), probe(NEW)]),
                                 timeout_s=15)
        self.assertTrue(result["valid"])
        self.assertTrue(result["read_only"])
        self.assertIsNone(result["external_trigger_host_s"])
        self.assertIsNone(result["reboot_request_host_s"])
        self.assertIsNone(result["request_to_system_complete_interval_s"])
        self.assertIn("external_trigger_unknown", result["measurement_boundary"])
        self.assertEqual(result["system_complete_interval_host_s"]["lower_reason"],
                         "last_new_boot_not_ready_probe_start")

    def test_new_boot_already_complete_is_upper_bound_without_fabricated_lower(self):
        result, _ = self.collect(Simulation([probe(), probe(NEW), probe(NEW)]), timeout_s=15)
        self.assertTrue(result["valid"])
        self.assertIsNone(result["system_complete_interval_host_s"]["lower"])
        self.assertEqual(result["system_complete_interval_host_s"]["lower_reason"],
                         "no_new_boot_not_ready_observed")
        self.assertEqual(result["new_boot_access_to_system_complete_interval_s"]["lower"], 0)
        self.assertIsNone(result["elapsed_s"])

    def test_old_completed_boot_and_usb_disconnect_reconnect_cannot_succeed(self):
        simulation = Simulation([probe(), OSError("USB disconnected"), probe(), probe()])
        result, files = self.collect(simulation, timeout_s=3)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "new_boot_system_complete_timeout")
        self.assertIsNone(result["new_boot_id"])
        self.assertIsNone(result["system_complete_interval_host_s"])
        self.assertIsNone(result["boot_id"])
        self.assertIsNone(result["request_to_system_complete_observed_s"])
        self.assertTrue(any("USB disconnected" in text for text in files.values()))

    def test_transport_failure_cannot_create_baseline_or_zero_duration(self):
        result, _ = self.collect(Simulation([OSError("adb not available")]), timeout_s=4)
        self.assertEqual(result["reason"], "baseline_boot_id_unavailable")
        self.assertIsNone(result["old_boot_id"])
        self.assertIsNone(result["elapsed_s"])
        self.assertEqual(result["failed_commands"][0]["error"], "transport_unavailable")

    def test_new_boot_not_completed_times_out_with_missing_endpoint(self):
        result, _ = self.collect(Simulation([probe(), probe(NEW, "0"), probe(NEW, "0")]),
                                 timeout_s=1.5)
        self.assertFalse(result["valid"])
        self.assertEqual(result["new_boot_id"], NEW)
        self.assertIsNone(result["system_complete_interval_host_s"])

    def test_two_new_boots_before_completion_are_invalid(self):
        result, _ = self.collect(Simulation([probe(), probe(NEW, "0"), probe(THIRD)]),
                                 timeout_s=15)
        self.assertEqual(result["reason"], "boot_id_changed_again")
        self.assertFalse(result["valid"])

    def test_boot_identity_change_inside_probe_is_invalid(self):
        result, _ = self.collect(Simulation([probe(), probe(NEW, after=THIRD)]), timeout_s=15)
        self.assertEqual(result["reason"], "boot_id_changed_during_probe")
        self.assertFalse(result["valid"])

    def test_boot_changed_while_auxiliary_data_collected_is_invalid(self):
        result, _ = self.collect(Simulation([probe(), probe(NEW), probe(THIRD)]), timeout_s=15)
        self.assertEqual(result["reason"], "boot_id_changed_during_collection")
        self.assertFalse(result["valid"])
        self.assertIsNotNone(result["system_complete_interval_host_s"])
        self.assertIsNone(result["elapsed_s"])

    def test_optional_bootstat_logs_failure_remains_nonfatal_and_raw(self):
        result, files = self.collect(Simulation([probe(), probe(NEW), probe(NEW)],
                                                diagnostics_fail=True), timeout_s=15)
        self.assertTrue(result["valid"])
        self.assertFalse(result["auxiliary"]["bootstat"]["available"])
        self.assertEqual(result["auxiliary"]["bootstat"]["reason"], "command_failed")
        self.assertTrue(any("permission denied" in text for text in files.values()))

    def test_timeout_limits_every_command_to_remaining_total_deadline(self):
        simulation = Simulation([probe(), (99, probe(NEW))])
        result, files = self.collect(simulation, timeout_s=1, command_timeout_s=3)
        self.assertFalse(result["valid"])
        self.assertAlmostEqual(result["host_end_s"], 11)
        self.assertAlmostEqual(simulation.calls[1]["timeout"], 0.8)
        self.assertLessEqual(simulation.calls[1]["timeout"],
                             result["host_deadline_s"] - simulation.calls[1]["start"])
        self.assertTrue(any("partial read" in text for text in files.values()))

    def test_no_budget_for_final_identity_does_not_claim_valid_measurement(self):
        result, _ = self.collect(Simulation([probe(), probe(NEW), (99, probe(NEW))]),
                                 timeout_s=0.5, command_timeout_s=3)
        self.assertEqual(result["reason"], "final_boot_id_unavailable")
        self.assertFalse(result["valid"])
        self.assertEqual(result["auxiliary"]["bootstat"]["reason"], "deadline_budget")

    def test_reboot_request_failure_is_saved_and_never_becomes_success(self):
        simulation = Simulation([probe()], reboot_error=True)
        result, _ = self.collect(simulation, reboot=True, timeout_s=5)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "reboot_request_failed")
        self.assertIsNone(result["new_boot_id"])

    def test_probe_rejects_missing_duplicate_and_nonfinite_uptime(self):
        for raw in ("", probe().replace("uptime=100", "uptime=nan"),
                    probe() + f"boot_id_before={OLD}\n"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_probe(raw)
        self.assertEqual(parse_probe(probe().replace("\n", "\r\n"))["boot_id"], OLD)

    def test_existing_directory_is_not_reused_and_invalid_config_never_calls_adb(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("abbench.boot.subprocess.run") as run:
                with self.assertRaises(FileExistsError):
                    measure_boot("device", temporary)
                for kwargs in ({"timeout_s": 0}, {"timeout_s": float("nan")},
                               {"poll_s": -1}, {"command_timeout_s": float("inf")},
                               {"reboot": "yes"}, {"timeout_s": True},
                               {"timeout_s": 301}, {"command_timeout_s": 6}, {"desktop": "yes"}):
                    with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                        measure_boot("device", Path(temporary) / "unused", **kwargs)
                run.assert_not_called()

    def test_optional_desktop_reports_resumed_interval_not_first_draw(self):
        simulation = DesktopSimulation([probe(), probe(NEW), probe(NEW)],
                                       [desktop_evidence(unlocked="false"), desktop_evidence()])
        result, _ = self.collect(simulation, reboot=True, desktop=True, timeout_s=15)
        self.assertTrue(result["valid"])
        self.assertTrue(result["desktop_resumed_observed"]["value"])
        self.assertFalse(result["desktop_resumed_observed"]["first_draw_verified"])
        self.assertIsNone(result["desktop_ready"]["value"])
        interval = result["request_to_desktop_resumed_interval_s"]
        self.assertLess(interval["lower"], interval["upper"])
        self.assertEqual(result["request_to_desktop_resumed_observed_s"], interval["upper"])

    def test_desktop_command_unavailable_preserves_valid_system_boot(self):
        simulation = DesktopSimulation([probe(), probe(NEW), probe(NEW)], ["cmd unsupported\n"])
        result, _ = self.collect(simulation, desktop=True, timeout_s=15)
        self.assertTrue(result["valid"])
        self.assertIsNone(result["desktop_resumed_observed"]["value"])
        self.assertEqual(result["desktop_resumed_observed"]["reason"], "desktop_evidence_fields_missing")
        self.assertIsNone(result["request_to_desktop_resumed_observed_s"])

    def test_desktop_history_mentions_and_ambiguous_home_do_not_prove_resumed(self):
        raw = desktop_evidence(resumed="com.example.other") + "Intent { cmp=com.example.launcher/.Launcher }\n"
        # History mention is within activity output, not the actual resumed field.
        raw = raw.replace("AB_DESKTOP_BOOT_AFTER", "History: com.example.launcher/.Launcher\nAB_DESKTOP_BOOT_AFTER")
        raw = raw.rsplit("Intent", 1)[0]
        self.assertFalse(parse_desktop_evidence(raw)["satisfied"])
        self.assertFalse(parse_desktop_evidence(desktop_evidence(animation="running"))["satisfied"])
        with self.assertRaises(ValueError):
            parse_desktop_evidence(desktop_evidence(home="com.example.launcher/.Launcher\ncom.example.other/.Home"))

    def test_desktop_new_boot_evidence_cannot_be_bound_to_another_boot(self):
        simulation = DesktopSimulation([probe(), probe(NEW)], [desktop_evidence(boot=THIRD)])
        result, _ = self.collect(simulation, desktop=True, timeout_s=15)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "desktop_observation_cross_boot")


if __name__ == "__main__":
    unittest.main()
