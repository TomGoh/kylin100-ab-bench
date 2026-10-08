import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.capture import _process_identity, _read_supply, clock_probe, mark_event, start_capture, stop_capture


BOOT = "953a8a32-df97-491c-81b1-327447eae8dc"
PHYSICAL = "T11206HXH6600007"
PROBE = {"boot_id": BOOT, "t_s": 100.0, "physical_serial": PHYSICAL}
PROCESS = {"starttime_ticks": 99, "executable": "/system/bin/perfetto"}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        mocked = patch("abbench.capture._read_supply", return_value={"valid": False, "reason": "fixture_supply_unavailable"})
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_supply_failure_is_retained_without_claiming_external_supply_off(self):
        module = types.ModuleType("abbench.supply")
        def unavailable(serial):
            raise subprocess.TimeoutExpired("fixture", 5, stderr="supply read timeout")
        module.read_supply = unavailable
        with patch.dict("sys.modules", {"abbench.supply": module}):
            result = _read_supply("192.0.2.7:5555")
        self.assertFalse(result["valid"])
        self.assertIn("supply_evidence_unavailable", result["reason"])
        self.assertNotIn("all_external_off", result)

    def test_wireless_clock_retains_physical_identity_and_transport_separately(self):
        raw = f"{PHYSICAL}\n{BOOT}\n100 0\n{BOOT}\n{PHYSICAL}\n"
        with patch("abbench.capture.shell", return_value=subprocess.CompletedProcess([], 0, raw, "")) as shell:
            result = clock_probe("192.0.2.7:5555")
        self.assertEqual(result["physical_serial"], PHYSICAL)
        self.assertEqual(result["transport_serial"], "192.0.2.7:5555")
        self.assertEqual(result["physical_serial_source"], "ro.serialno")
        self.assertEqual(shell.call_args.args[1].count("getprop ro.serialno"), 2)
        self.assertEqual(result["boot_id"], BOOT)
        self.assertEqual(result["t_s"], 100)

    def test_missing_changed_or_transport_serial_is_not_physical_identity(self):
        for first, last in (("", ""), (PHYSICAL, "OTHER"), ("unknown", "unknown"),
                            ("192.0.2.7:5555", "192.0.2.7:5555")):
            raw = f"{first}\n{BOOT}\n100 0\n{BOOT}\n{last}\n"
            with patch("abbench.capture.shell", return_value=subprocess.CompletedProcess([], 0, raw, "")):
                with self.assertRaises(ValueError):
                    clock_probe("192.0.2.7:5555")

    def test_same_directory_in_shell_arguments_cannot_identify_perfetto(self):
        remote = "/data/local/tmp/abbench-" + "a" * 32
        stat = "1234 (perfetto) " + " ".join(["S"] + ["0"] * 18 + ["99"] + ["0"] * 10)
        for executable in ("/system/bin/perfetto", "/system/bin/sh"):
            raw = stat + "\n" + executable + "\nperfetto\n" + remote + "/config.pbtxt\n" + remote + "/trace.perfetto-trace\n"
            with patch("abbench.capture.shell", return_value=subprocess.CompletedProcess([], 0, raw, "")):
                if executable.endswith("/sh"):
                    with self.assertRaises(ValueError):
                        _process_identity("serial", remote, 1234)
                else:
                    self.assertEqual(_process_identity("serial", remote, 1234)["starttime_ticks"], 99)

    def test_clock_probe_rejects_boot_change_and_nonfinite_time(self):
        for data in [f"{PHYSICAL}\n{BOOT}\n100 0\nother\n{PHYSICAL}\n",
                     f"{PHYSICAL}\n{BOOT}\nnan 0\n{BOOT}\n{PHYSICAL}\n"]:
            with patch("abbench.capture.shell", return_value=subprocess.CompletedProcess([], 0, data, "")):
                with self.assertRaises(ValueError):
                    clock_probe("serial")

    def test_background_start_requires_acknowledged_pid_and_new_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "capture"
            replies = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "1234\n", "")]
            with patch("abbench.capture.clock_probe", return_value=PROBE), patch("abbench.capture._process_identity", return_value=PROCESS), patch("abbench.capture.adb"), patch("abbench.capture.shell", side_effect=replies):
                result = start_capture("serial", target, 15)
            self.assertEqual(result["state"], "active")
            self.assertEqual(result["pid"], 1234)
            self.assertEqual(result["physical_serial"], PHYSICAL)
            self.assertEqual(result["transport_serial"], "serial")
            self.assertFalse(result["supply_before"]["valid"])
            self.assertIn("duration_ms: 15000", (target / "config.pbtxt").read_text())
            self.assertFalse(result["measurement_validated"])
            with self.assertRaises(FileExistsError):
                start_capture("serial", target, 15)

    def test_missing_pid_and_transport_failures_are_saved(self):
        for reply in [subprocess.CompletedProcess([], 0, "not-a-pid", "warning"),
                      subprocess.TimeoutExpired("adb", 35, output=b"partial-output", stderr=b"timeout-detail"),
                      subprocess.CalledProcessError(1, ["adb"], output="partial-output", stderr="CONFIG_PARSE_FAILED_DETAIL")]:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "capture"
                with patch("abbench.capture.clock_probe", return_value=PROBE), patch("abbench.capture.adb"), patch("abbench.capture.shell", side_effect=[subprocess.CompletedProcess([], 0, "", ""), reply]):
                    with self.assertRaises(ValueError):
                        start_capture("serial", target, 15)
                state = json.loads((target / "capture.json").read_text())
                self.assertEqual(state["state"], "failed")
                if isinstance(reply, subprocess.SubprocessError):
                    self.assertEqual((target / "failure.stdout.txt").read_text(), "partial-output")
                    self.assertIn("detail" if isinstance(reply, subprocess.TimeoutExpired) else "CONFIG_PARSE_FAILED_DETAIL", (target / "failure.stderr.txt").read_text())

    def test_stop_only_signals_its_private_session_and_preserves_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            remote = "/data/local/tmp/abbench-" + "a" * 32
            state = {"state": "active", "serial": "serial", "remote_dir": remote, "pid": 1234, "process": PROCESS, "before": PROBE}
            (target / "capture.json").write_text(json.dumps(state))
            def export(serial, remote, output):
                output.mkdir()
                (output / "trace.perfetto-trace").write_bytes(b"trace")
                return {"files": 1}
            with patch("abbench.capture.clock_probe", return_value=PROBE), patch("abbench.capture.shell", side_effect=[subprocess.CompletedProcess([], 0, "owned_session_stopped", ""), subprocess.CompletedProcess([], 0, "stopped", "")]) as run, patch("abbench.capture.pull_root", side_effect=export):
                result = stop_capture(target)
            self.assertEqual(result["state"], "exported")
            self.assertFalse(result["supply_after"]["valid"])
            self.assertIn("grep -F " + remote, run.call_args_list[0].args[1])
            self.assertIn("kill -TERM 1234", run.call_args_list[0].args[1])
            self.assertIn("getprop ro.serialno", run.call_args_list[0].args[1])

    def test_marker_and_stop_refuse_another_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "capture.json").write_text(json.dumps({"state": "active", "serial": "serial", "remote_dir": "/data/local/tmp/abbench-" + "a" * 32, "pid": 1234, "process": PROCESS, "before": PROBE}))
            with patch("abbench.capture.clock_probe", return_value={"boot_id": "another", "t_s": 5}), patch("abbench.capture.shell") as run:
                with self.assertRaises(ValueError):
                    mark_event(target, "compute_start")
                with self.assertRaises(ValueError):
                    stop_capture(target)
            run.assert_not_called()

    def test_start_failure_cleans_only_the_acknowledged_same_boot_session(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "capture"
            replies = [subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], 0, "1234\n", ""),
                       subprocess.CompletedProcess([], 0, "owned_session_stopped", "")]
            with patch("abbench.capture.clock_probe", side_effect=[PROBE, ValueError("probe failed")]), patch("abbench.capture._process_identity", return_value=PROCESS), patch("abbench.capture.adb"), patch("abbench.capture.shell", side_effect=replies) as run:
                with self.assertRaises(ValueError):
                    start_capture("serial", target, 15)
            state = json.loads((target / "capture.json").read_text())
            self.assertEqual(state["state"], "failed")
            self.assertTrue(state["cleanup"]["requested"])
            self.assertIn(BOOT, run.call_args_list[-1].args[1])
            self.assertIn(PHYSICAL, run.call_args_list[-1].args[1])
            self.assertIn("kill -TERM 1234", run.call_args_list[-1].args[1])

    def test_empty_uptime_becomes_value_error_and_post_export_boot_change_fails(self):
        with patch("abbench.capture.shell", return_value=subprocess.CompletedProcess([], 0, f"{PHYSICAL}\n{BOOT}\n\n{BOOT}\n{PHYSICAL}\n", "")):
            with self.assertRaises(ValueError):
                clock_probe("serial")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "capture.json").write_text(json.dumps({"state": "active", "serial": "serial", "remote_dir": "/data/local/tmp/abbench-" + "a" * 32, "pid": 1234, "process": PROCESS, "before": PROBE}))
            def export(serial, remote, output):
                output.mkdir()
                (output / "trace.perfetto-trace").write_bytes(b"trace")
                return {"files": 1}
            with patch("abbench.capture.clock_probe", side_effect=[PROBE, {"boot_id": "another", "t_s": 5}]), patch("abbench.capture.shell", side_effect=[subprocess.CompletedProcess([], 0, "stopped", ""), subprocess.CompletedProcess([], 0, "stopped", "")]), patch("abbench.capture.pull_root", side_effect=export):
                with self.assertRaises(ValueError):
                    stop_capture(target)
            self.assertEqual(json.loads((target / "capture.json").read_text())["state"], "failed")

    def test_same_boot_on_another_physical_device_cannot_mark_or_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            state = {"state": "active", "serial": "192.0.2.7:5555", "remote_dir": "/data/local/tmp/abbench-" + "a" * 32,
                     "pid": 1234, "process": PROCESS, "before": PROBE}
            (target / "capture.json").write_text(json.dumps(state))
            with patch("abbench.capture.clock_probe", return_value={**PROBE, "physical_serial": "OTHER"}), patch("abbench.capture.shell") as run:
                with self.assertRaises(ValueError):
                    mark_event(target, "end")
                with self.assertRaises(ValueError):
                    stop_capture(target)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
