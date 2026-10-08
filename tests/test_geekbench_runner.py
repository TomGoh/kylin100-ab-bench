import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from abbench import geekbench_runner as runner


def record(did=2, kind="cpu", **changes):
    document = {"uuid": "new-result", "version": "Geekbench 6.7.1", "valid": 1,
                "complete_benchmark": 1, "runtime": 649.7, "score": 700,
                "sections": [{"name": "Clang", "score": 650}]}
    if kind == "gpu":
        document.update(compute_api=4321, metrics=[{"value": "Vulkan 1.3 test-driver"}])
    result = {"document_id": did, "uuid": document["uuid"], "kind": kind,
              "version": document["version"], "valid": True, "complete": True,
              "exclusion_reasons": [], "document": document,
              "raw_json": json.dumps(document), "api_raw": 4321 if kind == "gpu" else None}
    result.update(changes)
    return result


def before():
    return {"records": [record(1, uuid="old-result")]}


def observation(state, t=400, boot="boot-a"):
    return {"boot_id": boot, "before_s": t, "after_s": t + 0.5,
            "payload": state, "nodes": [], "state": state}


class BindingTests(unittest.TestCase):
    def bind(self, rec, **kwargs):
        return runner.bind_result(before(), {"records": before()["records"] + [rec]},
                                  kind=kwargs.pop("kind", "cpu"), expected_version="6.7.1",
                                  boot_id="boot-a", end_boot_id=kwargs.pop("end_boot_id", "boot-a"),
                                  **kwargs)

    def test_positive_cpu_preserves_full_result(self):
        rec = record()
        result = self.bind(rec)
        self.assertEqual(result["uuid"], "new-result")
        self.assertEqual(result["document"], rec["document"])
        self.assertEqual(result["raw_json"], rec["raw_json"])
        self.assertEqual(result["previous_document_id"], 1)

    def test_gpu_uses_literal_result_evidence_not_enum_guess(self):
        result = self.bind(record(kind="gpu"), kind="gpu", api="Vulkan")
        self.assertEqual(result["api_raw"], 4321)
        self.assertEqual(result["api_name_verified"], "Vulkan")
        for rec in (record(kind="gpu"), record(kind="gpu")):
            rec["document"]["metrics"] = [{"value": "OpenCL 3.0 driver"}]
            with self.assertRaisesRegex(runner.RunnerError, "gpu_api_result_mismatch"):
                self.bind(rec, kind="gpu", api="Vulkan", result_ui="Vulkan Score")
        rec = record(kind="gpu")
        rec["document"]["metrics"] = []
        with self.assertRaisesRegex(runner.RunnerError, "unverified"):
            self.bind(rec, kind="gpu", api="Vulkan", result_ui="Vulkan Score")

    def test_stale_duplicate_wrong_kind_version_and_cross_boot_rejected(self):
        cases = [(record(1), "new_result_missing"),
                 (record(uuid="old-result"), "uuid"),
                 (record(kind="gpu"), "kind"),
                 (record(version="Geekbench 6.6.0"), "version"),
                 (record(valid=False, exclusion_reasons=["application_invalid"]), "invalid"),
                 (record(complete=False), "incomplete")]
        for rec, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(runner.RunnerError, reason):
                self.bind(rec)
        with self.assertRaisesRegex(runner.RunnerError, "cross_boot"):
            self.bind(record(), end_boot_id="boot-b")
        with self.assertRaisesRegex(runner.RunnerError, "uuid"):
            self.bind(record(), seen_uuids={"new-result"})
        with self.assertRaisesRegex(runner.RunnerError, "multiple_new"):
            runner.bind_result(before(), {"records": [record(2), record(3)]}, kind="cpu",
                               expected_version="6.7.1", boot_id="a", end_boot_id="a")

    def test_gpu_raw_json_and_database_api_must_agree(self):
        rec = record(kind="gpu")
        rec["document"]["compute_api"] = 5
        with self.assertRaisesRegex(runner.RunnerError, "raw_api"):
            self.bind(rec, kind="gpu", api="Vulkan")

    def test_software_gpu_is_rejected_and_hardware_identity_is_explicit(self):
        for software in ("SwiftShader", "llvmpipe", "lavapipe", "software renderer", "software device"):
            rec = record(kind="gpu")
            rec["document"]["compute_device_name"] = "Mali-G57 r0p1"
            rec["document"]["metrics"] = [{"value": "Vulkan1.3 " + software}]
            with self.subTest(software=software), self.assertRaisesRegex(runner.RunnerError, "software_gpu"):
                self.bind(rec, kind="gpu", api="Vulkan")
        rec = record(kind="gpu")
        rec["document"].update(compute_device_name="Mali-G57 r0p1", compute_platform_name="ARM ARM Platform",
                               compute_driver_version="r54p1")
        result = self.bind(rec, kind="gpu", api="Vulkan")
        self.assertTrue(result["gpu_hardware_verified"])
        self.assertEqual(result["gpu_identity"]["driver_fields"], {"compute_driver_version": "r54p1"})
        rec["document"]["compute_device_name"] = "unknown device"
        self.assertFalse(self.bind(rec, kind="gpu", api="Vulkan")["gpu_hardware_verified"])


class UiTests(unittest.TestCase):
    # Resource IDs, title, package and labels taken from saved CPU validation XML.
    DIALOG = ('<hierarchy><node resource-id="android:id/parentPanel" package="com.primatelabs.geekbench6">'
              '<node resource-id="android:id/alertTitle" text="Geekbench 6" package="com.primatelabs.geekbench6"/>'
              '<node resource-id="android:id/message" text="Running Object Detection" package="com.primatelabs.geekbench6"/>'
              '<node resource-id="android:id/progress_percent" text="27%" package="com.primatelabs.geekbench6"/>'
              '<node resource-id="android:id/button2" text="CANCEL" enabled="true" package="com.primatelabs.geekbench6"/>'
              '</node></hierarchy>')

    def test_real_workload_dialog_is_computing_including_named_workloads(self):
        for workload in ("Object Detection", "Asset Compression", "Horizon Detection", "GPU Benchmark"):
            xml = self.DIALOG.replace("Object Detection", workload)
            self.assertEqual(runner.classify_ui(runner.ui_nodes(xml)), "computing")
        # A result page underneath the current dialog must not terminate the run.
        xml = self.DIALOG.replace('</hierarchy>', '<node resource-id="com.primatelabs.geekbench6:id/resultsFrame"/></hierarchy>')
        self.assertEqual(runner.classify_ui(runner.ui_nodes(xml)), "computing")

    def test_running_text_and_confusing_dialogs_are_not_compute_evidence(self):
        mutations = [self.DIALOG.replace("Geekbench 6", "Download Manager"),
                     self.DIALOG.replace("com.primatelabs.geekbench6", "other.application"),
                     self.DIALOG.replace("27%", "101%"),
                     self.DIALOG.replace("android:id/progress_percent", "other:id/progress_percent"),
                     self.DIALOG.replace('text="CANCEL"', 'text="OK"'),
                     self.DIALOG.replace('enabled="true"', 'enabled="false"'),
                     self.DIALOG.replace("Running Object Detection", "Loading Results"),
                     '<hierarchy><node text="Running GPU Benchmark"/></hierarchy>']
        for xml in mutations:
            with self.subTest(xml=xml):
                self.assertEqual(runner.classify_ui(runner.ui_nodes(xml)), "unknown")

    def test_resource_location_spinner_and_error_states(self):
        nodes = runner.ui_nodes('<hierarchy><node resource-id="com.primatelabs.geekbench6:id/computeApiSpinner" bounds="[10,20][80,60]" enabled="true"><node text="Vulkan"/></node><node resource-id="com.primatelabs.geekbench6:id/runComputeBenchmark" bounds="[10,90][100,150]"/></hierarchy>')
        self.assertEqual(runner.selected_api(nodes), "Vulkan")
        self.assertEqual(runner._center(runner.find_target(nodes, resource="runComputeBenchmark")), (55, 120))
        self.assertEqual(runner.classify_ui(nodes), "home")
        for text, state in [("Uploading Results", "uploading"),
                            ("Upload Failed", "upload_failed"), ("CPU Error", "benchmark_failed")]:
            self.assertEqual(runner.classify_ui(runner.ui_nodes('<hierarchy><node text="' + text + '"/></hierarchy>')), state)
        with self.assertRaisesRegex(runner.RunnerError, "ambiguous"):
            runner.find_target(nodes + [copy.deepcopy(nodes[0])], resource="computeApiSpinner")
        with self.assertRaises(runner.RunnerError):
            runner.ui_nodes("old XML not present")

    def test_scalar_document_insert_is_not_completion(self):
        device = object.__new__(runner._Device)
        device.sqlite_available = True
        replies = [observation("", t=400), observation("", t=401)]
        replies[0]["payload"] = "2"
        replies[1]["payload"] = json.dumps({"complete_benchmark": 0})
        with patch.object(device, "timed", side_effect=replies):
            self.assertFalse(device.new_document_id(1)["new"])
        replies[1]["payload"] = json.dumps({"complete_benchmark": 1})
        with patch.object(device, "timed", side_effect=replies):
            self.assertTrue(device.new_document_id(1)["new"])

    def test_transient_empty_dump_retries_new_snapshot_without_tap(self):
        device = object.__new__(runner._Device)
        device.remote, device.index, device.deadline = "/private", 0, None
        empty, good = observation(""), observation("")
        good["payload"] = self.DIALOG
        with patch.object(device, "timed", side_effect=[empty, good]) as timed, patch("abbench.geekbench_runner.time.sleep"):
            self.assertEqual(device.observe("home")["state"], "computing")
            self.assertEqual(timed.call_count, 2)
            self.assertNotEqual(timed.call_args_list[0].args[0], timed.call_args_list[1].args[0])
            self.assertTrue(all("input tap" not in call.args[0] for call in timed.call_args_list))
        with patch.object(device, "timed", return_value=empty) as timed, patch("abbench.geekbench_runner.time.sleep"):
            with self.assertRaisesRegex(runner.RunnerError, "ui_xml_missing"):
                device.observe("home")
            self.assertEqual(timed.call_count, 3)

    def test_tab_label_selects_unique_clickable_parent_not_text_child(self):
        nodes = runner.ui_nodes('<hierarchy><node content-desc="GPU" clickable="true" bounds="[960,143][1140,215]"><node text="GPU" clickable="false" bounds="[1029,164][1071,193]"/></node></hierarchy>')
        self.assertEqual(runner._center(runner.find_target(nodes, label="GPU")), (1050, 179))
        with self.assertRaisesRegex(runner.RunnerError, "ambiguous"):
            runner.find_target(nodes + [copy.deepcopy(nodes[0])], label="GPU")


class FakeDevice:
    states = ["computing", "uploading"]
    query_sequence = [False, False, True]
    output_record = record()
    end_boot = "boot-a"
    failure_called = False

    def __init__(self, serial, out, timeout):
        self.sqlite_available = True
        self.queries = iter(type(self).query_sequence)
        self.states_iter = iter(type(self).states)
        self.identity_count = 0

    def identity(self):
        self.identity_count += 1
        return observation("identity", boot="boot-a" if self.identity_count == 1 else self.end_boot)

    def metadata(self):
        return {"version": "6.7.1", "sqlite_available": True}

    def snapshot(self, name, allow_stop=False):
        data = before() if name == "history-before" else {"records": before()["records"] + [self.output_record]}
        return data, {"method": "sqlite_online_backup", "integrity_checked": True}

    def start(self, kind, api):
        return observation("started", t=410)

    def new_document_id(self, baseline):
        return {**observation("query", t=420), "new": next(self.queries, False)}

    def observe(self, label):
        return observation(next(self.states_iter, "computing"), t=415)

    def failure_evidence(self):
        type(self).failure_called = True


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        FakeDevice.states = ["computing", "uploading"]
        FakeDevice.query_sequence = [False, False, True]
        FakeDevice.output_record = record()
        FakeDevice.end_boot = "boot-a"
        FakeDevice.failure_called = False

    def run_fixture(self, **kwargs):
        out = Path(self.temp.name) / "run-1"
        clock = kwargs.pop("clock", [0])
        with patch.object(runner, "_Device", FakeDevice), patch.object(runner.time, "sleep"), \
             patch.object(runner.time, "monotonic", side_effect=itertools.chain(clock, itertools.repeat(clock[-1]))):
            # Provide a real finite clock for timeout controls when a sequence is requested.
            if "compute_timeout_s" not in kwargs:
                kwargs["compute_timeout_s"] = 1200
            result = runner.run_geekbench("fixture-serial", "cpu", out, mode="native", **kwargs)
        return result, out

    def test_run_separates_compute_and_persistence_and_keeps_json(self):
        result, out = self.run_fixture(clock=[0, 1, 2, 3, 4])
        self.assertTrue(result["valid"])
        self.assertTrue(result["compute_complete_observed"])
        self.assertEqual(result["internal_runtime_s"], 649.7)
        self.assertEqual(json.loads((out / "result.json").read_text()), FakeDevice.output_record["document"])
        self.assertIn("compute_complete", [event["state"] for event in result["events"]])
        self.assertIn("persisted", [event["state"] for event in result["events"]])
        self.assertFalse(result["measurement_validated"])
        with self.assertRaises(FileExistsError):
            runner.run_geekbench("fixture-serial", "cpu", out)

    def test_upload_failure_retains_failed_run_not_old_score(self):
        FakeDevice.states = ["upload_failed"]
        FakeDevice.query_sequence = [False]
        result, out = self.run_fixture(clock=[0, 1])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "upload_failed_result_not_saved")
        self.assertTrue(result["compute_complete_observed"])
        self.assertTrue((out / "failure.json").exists())
        self.assertTrue(FakeDevice.failure_called)
        self.assertFalse((out / "result.json").exists())

    def test_timeout_and_cross_boot_never_report_success(self):
        FakeDevice.query_sequence = [False]
        FakeDevice.states = ["computing"]
        result, _ = self.run_fixture(clock=[0, 2], compute_timeout_s=1)
        self.assertFalse(result["valid"])
        self.assertIn("timeout", result["reason"])
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "_Device", FakeDevice), \
             patch.object(runner.time, "sleep"), patch.object(runner.time, "monotonic", side_effect=[0, 1, 2, 3]):
            FakeDevice.query_sequence = [True]
            FakeDevice.end_boot = "boot-b"
            result = runner.run_geekbench("fixture-serial", "cpu", Path(directory) / "crossboot")
            self.assertFalse(result["valid"])
            self.assertEqual(result["reason"], "cross_boot_result")

    def test_late_persisted_result_cannot_bypass_stage_deadline(self):
        FakeDevice.query_sequence = [False, True]
        FakeDevice.states = ["computing"]
        # Start at zero. The first query is timely, the second completes at 3s.
        class ClockDevice(FakeDevice):
            clock = 0
            query_count = 0

            def new_document_id(self, baseline):
                type(self).query_count += 1
                type(self).clock = 1 if self.query_count == 1 else 3
                return super().new_document_id(baseline)

        with patch.object(runner, "_Device", ClockDevice), patch.object(runner.time, "sleep") as sleep, \
             patch.object(runner.time, "monotonic", side_effect=lambda: ClockDevice.clock):
            result = runner.run_geekbench("fixture", "cpu", Path(self.temp.name) / "late", compute_timeout_s=2)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "compute_or_unobserved_persistence_timeout")
        self.assertEqual(sleep.call_args.args, (1,))
        self.assertIn("failure_snapshot", result)

    def test_persistence_deadline_cannot_be_extended_by_poll_sleep(self):
        class ClockDevice(FakeDevice):
            states = ["uploading"]
            query_sequence = [False, True]
            clock = 0
            query_count = 0

            def new_document_id(self, baseline):
                type(self).query_count += 1
                type(self).clock = 1 if self.query_count == 1 else 5
                return super().new_document_id(baseline)

        with patch.object(runner, "_Device", ClockDevice), patch.object(runner.time, "sleep") as sleep, \
             patch.object(runner.time, "monotonic", side_effect=lambda: ClockDevice.clock):
            result = runner.run_geekbench("fixture", "cpu", Path(self.temp.name) / "late-persist", persist_timeout_s=2)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "persistence_timeout_after_compute")
        self.assertEqual(sleep.call_args.args, (2,))

    def test_each_adb_call_uses_remaining_budget_and_late_stdout_is_preserved(self):
        import subprocess
        for ended, expected_error in [(9, False), (11, True)]:
            with tempfile.TemporaryDirectory() as directory:
                device = runner._Device("fixture", directory, 20)
                device.deadline, device.deadline_reason = 10, "compute_timeout"
                with patch.object(runner.time, "monotonic", side_effect=[5, ended]), \
                     patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "late-result-json", "")) as command:
                    if expected_error:
                        with self.assertRaisesRegex(runner.RunnerError, "compute_timeout"):
                            device.shell("true", "budget")
                    else:
                        self.assertEqual(device.shell("true", "budget"), "late-result-json")
                self.assertEqual(command.call_args.kwargs["timeout"], 5)
                self.assertEqual((Path(directory) / "commands/0001-budget.stdout.txt").read_text(), "late-result-json")


if __name__ == "__main__":
    unittest.main()
