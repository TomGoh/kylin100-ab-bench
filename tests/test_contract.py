import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.doctor import snapshot
from abbench.manifest import validate_manifest


class ContractTests(unittest.TestCase):
    def manifest(self):
        common = {
            "host_kernel_sha256": "c" * 64, "kernel_runtime_id": "kernel-notes",
            "android_fingerprint": "same-android", "app_apk_sha256": "d" * 64,
            "app_version": "6.7.1", "mode_evidence": "saved-readback.json",
            "normal_configuration_confirmed": True, "identity_verified_on_device": True,
        }
        return {"images": {
            "native": {**common, "image_sha256": "a" * 64},
            "xhyper": {**common, "image_sha256": "b" * 64,
                       "xhyper_commit": "commit-a", "manager_commit": "commit-b",
                       "build_configuration": "normal-config.json", "diagnostic_features": []},
        }}

    def test_matching_handoff_and_mismatched_kernel(self):
        data = self.manifest()
        self.assertTrue(validate_manifest(data)["valid"])
        data["images"]["xhyper"]["host_kernel_sha256"] = "e" * 64
        self.assertIn("pair:mismatched_host_kernel_sha256", validate_manifest(data)["issues"])

    def test_unverified_and_diagnostic_images_are_rejected(self):
        data = self.manifest()
        data["images"]["xhyper"]["diagnostic_features"] = ["periodic-counters"]
        self.assertFalse(validate_manifest(data)["valid"])
        data = self.manifest()
        data["images"]["native"]["identity_verified_on_device"] = False
        self.assertIn("native:device_identity_unconfirmed", validate_manifest(data)["issues"])

    def test_example_is_deliberately_not_ready(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "image-manifest.json"
        self.assertFalse(validate_manifest(json.loads(path.read_text()))["valid"])

    def test_probe_saves_timeout_and_never_confirms_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.CompletedProcess([], 0, "raw output", "")
            fail = subprocess.TimeoutExpired(["adb"], 1, output=b"partial")
            with patch("abbench.doctor.COMMANDS", {"identity": "id", "battery": "dumpsys battery"}), patch(
                "abbench.doctor.subprocess.run", side_effect=[proc, fail]
            ) as run:
                result = snapshot("serial", Path(directory) / "snapshot", 1)
            self.assertFalse(result["acquisition_complete"])
            self.assertFalse(result["capabilities_verified"])
            self.assertTrue(result["commands"]["battery"]["timed_out"])
            self.assertEqual(run.call_args_list[0].args[0][-1], "id")
            self.assertEqual((Path(directory) / "snapshot/battery.txt").read_text(), "partial")
            with self.assertRaises(FileExistsError):
                snapshot("serial", Path(directory) / "snapshot", 1)

    def test_cli_has_no_comparable_data_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.json"
            path.write_text("[]")
            proc = subprocess.run(["python3", "-m", "abbench", "compare", "--input", str(path)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(json.loads(proc.stdout)["has_comparable_groups"])


if __name__ == "__main__":
    unittest.main()
