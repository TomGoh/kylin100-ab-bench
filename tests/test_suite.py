import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from abbench import suite
from abbench.device_lock import DeviceLock


H = "a" * 64
PROFILE = {"wait_after_boot_s": 300, "identity_readback": {"slot_suffix": "_a", "mode_evidence_source": "cmdline",
           "mode_evidence_token": {"native": None, "xhyper": "xhyper.mode=host"}}}
MANIFEST = {"images": {mode: {"image_sha256": H, "kernel_runtime_id": H, "android_fingerprint": "Android fixture",
             "app_apk_sha256": H, "app_version": "6.7.1", "mode_evidence": "signed image delivery"} for mode in ("native", "xhyper")}}
IDENTITY = {"valid": True, "image_sha256": H, "kernel_runtime_id": H, "fingerprint": "Android fixture",
            "cmdline": "xhyper.mode=host", "slot": "_a", "boot_id": "boot-fixture"}


class Backend:
    def __init__(self, fail_cpu=False, fail_stop=False, unknown_gpu=False):
        self.calls = []
        self.fail_cpu, self.fail_stop, self.unknown_gpu = fail_cpu, fail_stop, unknown_gpu
        self.boot_index = 0
        self.current_boot = "boot-fixture"

    def call(self, module, function, *args, **kwargs):
        self.calls.append((module, function, args, kwargs))
        if module == "manifest":
            return {"valid": True, "issues": []}
        if module == "report":
            return {"created": True, "validation_only": kwargs["campaign"]["purpose"] == "validation"}
        if module == "boot":
            self.boot_index += 1
            self.current_boot = "new-boot-" + str(self.boot_index)
            return {"valid": True, "boot_id": self.current_boot, "request_to_system_complete_observed_s": 35,
                    "measurement_boundary": "warm_reboot_proxy"}
        if module == "environment":
            if function == "capture_environment":
                return {"valid": True, "boot_id": self.current_boot, "uptime_s": 400, "battery_temperature_c": 30,
                        "settings": {"screen_brightness": "100", "screen_brightness_mode": "0",
                                     "screen_off_timeout": "300000", "stay_on_while_plugged_in": "0"}}
            if function == "thermal_gate":
                return {"valid": True, "temperature_c": 30}
            return {"valid": True, "boot_id": self.current_boot,
                    "aggregate": {"mean_mem_available_bytes": 400, "mean_estimated_unavailable_bytes": 600}, "samples": []}
        if module == "idle":
            return {"valid": True, "boot_id": self.current_boot, "kind_observed": kwargs["kind"],
                    "power": {"valid": False, "reason": "counter_unverified"},
                    "capture_window_start": {"t_s": 400}, "capture_window_end": {"t_s": 410}}
        if module == "capture":
            if function == "clock_probe":
                return {"boot_id": self.current_boot, "t_s": 400}
            if function == "stop_capture" and self.fail_stop:
                raise ValueError("capture cleanup failure")
            return {"state": "active" if function == "start_capture" else "exported"}
        if module == "analysis":
            return {"valid": True, "power": {"valid": True, "mean_w": 0.2, "energy_j": 2, "power_boundary": "battery_net"},
                    "memory": {"valid": True, "mem_available_sampled_min_bytes": 350, "estimated_unavailable_mean_bytes": 650,
                               "measurement_boundary": "android_kernel_visible_memory_sampled_subwindow",
                               "actual_sampled_window_start_s": 425, "actual_sampled_window_end_s": 430,
                               "requested_window_start_s": kwargs.get("start_s"), "requested_window_end_s": kwargs.get("end_s"),
                               "actual_sample_gap_max_s": 5, "sample_count": 2}}
        if module == "geekbench_runner":
            kind = args[1]
            if self.fail_cpu and kind == "cpu":
                raise ValueError("CPU run timeout and pending app")
            return {"valid": True, "kind": kind, "boot_id": self.current_boot,
                    "gpu_hardware_verified": kind == "gpu" and not self.unknown_gpu,
                    "app": {"version": "6.7.1", "apk_sha256": H}, "result_uuid": kind + "-uuid",
                    "requested_api": "Vulkan" if kind == "gpu" else None,
                    "result": {"metrics": {"single": 700, "multi": 2000} if kind == "cpu" else {"score": 4000},
                               "api_raw": None if kind == "cpu" else 4321,
                               "document": {"sections": [{"name": "Single-Core", "workloads": [{"name": "Clang", "score": 650}]}]}},
                    "start_request_window": {"before_s": 420, "after_s": 420.5},
                    "compute_complete_window": {"lower_s": 429, "upper_s": 430},
                    "persisted_window": {"after_s": 431}}
        raise AssertionError((module, function))


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def run_fixture(self, backend=None, **kwargs):
        backend = backend or Backend()
        identity = kwargs.pop("identity", IDENTITY)
        with patch.object(suite, "_call", side_effect=backend.call), patch.object(suite, "read_identity", return_value=identity), \
             patch.object(suite.time, "sleep"), patch.object(suite, "_deadline", return_value=kwargs.pop("deadline", float("inf"))):
            result = suite.run_suite("fixture", Path(self.temp.name) / "suite", "xhyper", "processor", PROFILE,
                                     manifest=MANIFEST, **kwargs)
        return result, backend

    def test_full_serial_sequence_uses_existing_modules_and_labels_validation(self):
        result, backend = self.run_fixture(validation_only=True, options={"recovery_s": 0})
        self.assertTrue(result["valid"])
        self.assertTrue(result["report"]["validation_only"])
        names = [(module, function) for module, function, _, _ in backend.calls]
        starts = [i for i, name in enumerate(names) if name == ("capture", "start_capture")]
        runs = [i for i, name in enumerate(names) if name == ("geekbench_runner", "run_geekbench")]
        stops = [i for i, name in enumerate(names) if name == ("capture", "stop_capture")]
        self.assertEqual(len(starts), 2)
        self.assertTrue(starts[0] < runs[0] < stops[0] < starts[1] < runs[1] < stops[1])
        self.assertTrue(all(row["configuration"]["purpose"] == "validation" for row in result["metric_rows"]))
        self.assertIn("geekbench_cpu_Single-Core_Clang", [row["metric"] for row in result["metric_rows"]])
        self.assertFalse(result["identity_verified"])
        memory = next(row for row in result["metric_rows"] if row["metric"] == "geekbench_cpu_mem_available_sampled_min_bytes")
        self.assertEqual(memory["measurement_boundary"], "android_kernel_visible_memory_sampled_subwindow")
        self.assertEqual(memory["memory_window"]["actual_sampled_window_start_s"], 425)
        self.assertEqual(memory["memory_window"]["actual_sample_gap_max_s"], 5)
        self.assertEqual(memory["api"], None)
        self.assertEqual(memory["apk_sha256"], H)
        self.assertTrue(all(row["purpose"] == "validation" and row["validation_only"] for row in result["metric_rows"]))

    def test_wrong_image_or_slot_refuses_benchmarks(self):
        for changes in ({"image_sha256": "b" * 64}, {"slot": "_b"}):
            with tempfile.TemporaryDirectory() as temporary:
                self.temp.name = temporary
                result, backend = self.run_fixture(identity={**IDENTITY, **changes})
            self.assertFalse(result["valid"])
            self.assertFalse(any(module == "geekbench_runner" for module, *_ in backend.calls))

    def test_workload_failure_stops_owned_capture_and_does_not_start_next_gpu(self):
        result, backend = self.run_fixture(Backend(fail_cpu=True), validation_only=True)
        self.assertFalse(result["valid"])
        self.assertEqual(sum(function == "stop_capture" for _, function, *_ in backend.calls), 1)
        self.assertEqual(sum(module == "geekbench_runner" for module, *_ in backend.calls), 1)
        self.assertIn("previous_workload", result["skipped"][-1]["reason"])

    def test_hardware_unknown_is_retained_but_never_formal_gpu_score(self):
        result, _ = self.run_fixture(Backend(unknown_gpu=True))
        gpu = [row for row in result["metric_rows"] if row["metric"] == "geekbench_gpu_score"]
        self.assertEqual(gpu[0]["value"], 4000)
        self.assertFalse(gpu[0]["valid"])
        self.assertEqual(gpu[0]["reason"], "gpu_hardware_identity_unverified")

    def test_deadline_does_not_begin_incomplete_coverage(self):
        with patch.object(suite.time, "monotonic", return_value=1):
            result, backend = self.run_fixture(validation_only=True, deadline=2)
        self.assertFalse(result["valid"])
        self.assertFalse(any(module in ("environment", "idle", "geekbench_runner") for module, *_ in backend.calls))
        self.assertIn("deadline_insufficient_budget", [item["reason"] for item in result["skipped"]])

    def test_bad_manifest_is_rejected_before_device_modules(self):
        backend = Backend()
        original = backend.call
        def invalid(module, function, *args, **kwargs):
            if module == "manifest":
                return {"valid": False, "issues": ["normal_configuration_unconfirmed"]}
            return original(module, function, *args, **kwargs)
        with patch.object(suite, "_call", side_effect=invalid):
            result = suite.run_suite("fixture", Path(self.temp.name) / "invalid", "native", "processor", PROFILE, manifest={})
        self.assertFalse(result["valid"])
        self.assertFalse(any(module not in ("report",) for module, *_ in backend.calls))

    def test_campaign_requires_adapter_and_covers_both_modes_before_repeats(self):
        with self.assertRaises(ValueError):
            suite.run_campaign("fixture", Path(self.temp.name) / "invalid", "tp", PROFILE, manifest=MANIFEST, adapter=None)
        order = []
        def one(serial, directory, mode, processor, profile, **kwargs):
            order.append(mode)
            return {"valid": True, "temperature_baseline_c": 30, "metric_rows": [], "failed_runs": [], "skipped": []}
        backend = Backend()
        def switch(*args, **kwargs):
            backend.current_boot += "-new"
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(suite, "run_suite", side_effect=one), patch.object(suite, "_call", side_effect=backend.call), \
             patch.object(suite.subprocess, "run", side_effect=switch) as adapter:
            suite.run_campaign("fixture", Path(self.temp.name) / "campaign", "tp", PROFILE, manifest=MANIFEST,
                               adapter=["provided-adapter", "{mode}", "{serial}", "{out}"], repetitions=2, validation_only=True)
        self.assertEqual(order, ["native", "xhyper", "native", "xhyper"])
        self.assertFalse(adapter.call_args.kwargs["shell"])

    def test_noop_adapter_and_unconfigured_positive_mode_evidence_rejected(self):
        backend = Backend()
        with patch.object(suite, "_call", side_effect=backend.call), patch.object(suite, "run_suite") as run, \
             patch.object(suite.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = suite.run_campaign("fixture", Path(self.temp.name) / "noop", "tp", PROFILE,
                                        manifest=MANIFEST, adapter=["noop"], validation_only=True)
        run.assert_not_called()
        self.assertTrue(all("new_boot" in entry["reason"] for entry in result["failed_runs"]))
        profile = copy.deepcopy(PROFILE)
        profile["identity_readback"]["mode_evidence_token"]["xhyper"] = None
        self.assertFalse(suite.verify_identity(IDENTITY, MANIFEST, "xhyper", profile)["valid"])
        self.assertFalse(suite.verify_identity(IDENTITY, MANIFEST, "native", PROFILE)["valid"])

    def test_settings_pair_mismatch_prevents_workload_and_lifecycle_boundary_is_explicit(self):
        result, backend = self.run_fixture(options={"settings_baseline": {"screen_brightness": "200"}})
        self.assertFalse(result["valid"])
        self.assertFalse(any(module == "geekbench_runner" for module, *_ in backend.calls))
        with tempfile.TemporaryDirectory() as temporary:
            self.temp.name = temporary
            result, _ = self.run_fixture(validation_only=True)
        self.assertEqual(result["cpu_window"]["boundary"], "click_to_result_persisted_observed_lifecycle")
        row = next(row for row in result["metric_rows"] if row["metric"] == "geekbench_cpu_energy_j")
        self.assertEqual(row["measurement_boundary"], "click_to_result_persisted_observed_lifecycle")

    def test_device_lock_is_reentrant_but_refuses_another_process(self):
        with DeviceLock("lock-unit-test"), DeviceLock("lock-unit-test"):
            proc = subprocess.run([sys.executable, "-c", "from abbench.device_lock import DeviceLock; DeviceLock('lock-unit-test').__enter__()"],
                                  capture_output=True, text=True, timeout=5)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("device_already_locked", proc.stderr)
        with DeviceLock("lock-unit-test"):
            pass

    def test_all_unknown_settings_cannot_pass_formal_gate(self):
        backend = Backend()
        original = backend.call
        def unknown(module, function, *args, **kwargs):
            result = original(module, function, *args, **kwargs)
            if module == "environment" and function == "capture_environment":
                result["settings"] = {key: None for key in result["settings"]}
            return result
        with patch.object(suite, "_call", side_effect=unknown), patch.object(suite, "read_identity", return_value=IDENTITY):
            result = suite.run_suite("fixture", Path(self.temp.name) / "unknown-settings", "xhyper", "tp", PROFILE, manifest=MANIFEST)
        self.assertFalse(result["settings_verified"])
        self.assertFalse(result["valid"])
        self.assertFalse(any(module == "geekbench_runner" for module, *_ in backend.calls))

    def test_user_interruption_never_switches_next_image(self):
        backend = Backend()
        def switch(*args, **kwargs):
            backend.current_boot += "-new"
            return subprocess.CompletedProcess([], 0, "", "")
        stopped = {"valid": False, "temperature_baseline_c": 30, "metric_rows": [], "skipped": [],
                   "failed_runs": [{"name": "suite", "reason": "interrupted"}]}
        with patch.object(suite, "run_suite", return_value=stopped), patch.object(suite, "_call", side_effect=backend.call), \
             patch.object(suite.subprocess, "run", side_effect=switch) as adapter:
            result = suite.run_campaign("fixture", Path(self.temp.name) / "interrupted", "tp", PROFILE,
                                        manifest=MANIFEST, adapter=["provided-adapter"], repetitions=2, validation_only=True)
        adapter.assert_called_once()
        self.assertFalse(result["valid"])
        self.assertEqual(result["skipped"][-1]["reason"], "interrupted_no_more_mode_switches")

    def test_three_boot_samples_use_three_actual_new_identities(self):
        result, backend = self.run_fixture(validation_only=True, options={"reboot": True, "boot_repeats": 3})
        self.assertTrue(result["valid"])
        rows = [row for row in result["metric_rows"] if row["metric"] == "warm_reboot_to_system_complete_s"]
        self.assertEqual({row["boot_id"] for row in rows}, {"new-boot-1", "new-boot-2", "new-boot-3"})
        self.assertEqual(backend.boot_index, 3)


if __name__ == "__main__":
    unittest.main()
