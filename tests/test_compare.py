import copy
import math
import unittest

from abbench.compare import summarize


def sample(mode, value, run_id, boot_id="boot-1", metric="cpu.single", **extra):
    return {"mode": mode, "value": value, "run_id": run_id, "boot_id": boot_id,
            "metric": metric, "valid": True, "version": "6.7.1", **extra}


class ComparisonTests(unittest.TestCase):
    def test_known_positive_difference_and_stats(self):
        rows = [sample("native", 100, "n1", "nb1"), sample("native", 200, "n2", "nb2"),
                sample("xhyper", 180, "x1", "xb1"), sample("xhyper", 240, "x2", "xb1")]
        before = copy.deepcopy(rows)
        output = summarize(rows)
        self.assertEqual(rows, before)
        group = output["groups"][0]
        self.assertEqual(group["native"]["values"], [100, 200])
        self.assertEqual(group["native"]["mean"], 150)
        self.assertEqual(group["native"]["median"], 150)
        self.assertAlmostEqual(group["native"]["sample_sd"], math.sqrt(5000))
        self.assertEqual(group["native"]["range"], 100)
        self.assertEqual(group["native"]["distinct_boot_count"], 2)
        self.assertEqual(group["xhyper"]["distinct_boot_count"], 1)
        self.assertAlmostEqual(group["delta_pct"], 40)
        self.assertIsNone(group["confidence_interval"])

    def test_api_and_version_groups_cannot_be_combined(self):
        rows = [sample("native", 100, "n-vk", api="Vulkan"),
                sample("xhyper", 110, "x-vk", api="Vulkan"),
                sample("native", 200, "n-cl", api="OpenCL"),
                sample("xhyper", 300, "x-cl", api="OpenCL"),
                sample("native", 500, "n-new", api="Vulkan", version="7.0")]
        groups = summarize(rows)["groups"]
        self.assertEqual(len(groups), 3)
        by_api_version = {(g["comparison_key"]["api"], g["comparison_key"]["version"]): g for g in groups}
        self.assertAlmostEqual(by_api_version[("Vulkan", "6.7.1")]["delta_pct"], 10)
        self.assertAlmostEqual(by_api_version[("OpenCL", "6.7.1")]["delta_pct"], 50)
        self.assertIsNone(by_api_version[("Vulkan", "7.0")]["delta_pct"])

    def test_comparison_key_and_measurement_boundary_partition(self):
        rows = [sample("native", 1, "n1", comparison_key={"scene": "idle"}, unit="W"),
                sample("xhyper", 2, "x1", comparison_key={"scene": "idle"}, unit="W"),
                sample("native", 3, "n2", comparison_key={"scene": "load"}, unit="W"),
                sample("xhyper", 4, "x2", comparison_key={"scene": "idle"}, unit="W", measurement_boundary="battery-net")]
        self.assertEqual(len(summarize(rows)["groups"]), 3)

    def test_power_boundary_and_workload_identity_cannot_be_combined(self):
        rows = [sample("native", 1, "n", power_boundary="battery_net",
                       workload_version="1", apk_sha256="apk-a"),
                sample("xhyper", 2, "x-same", power_boundary="battery_net",
                       workload_version="1", apk_sha256="apk-a"),
                sample("xhyper", 3, "x-device", power_boundary="battery_side_device",
                       workload_version="1", apk_sha256="apk-a"),
                sample("xhyper", 4, "x-workload", power_boundary="battery_net",
                       workload_version="2", apk_sha256="apk-a"),
                sample("xhyper", 5, "x-apk", power_boundary="battery_net",
                       workload_version="1", apk_sha256="apk-b")]
        groups = summarize(rows)["groups"]
        self.assertEqual(len(groups), 4)
        comparable = [group for group in groups if group["delta_pct"] is not None]
        self.assertEqual(len(comparable), 1)
        self.assertEqual(comparable[0]["delta_pct"], 100)

    def test_different_mode_and_image_hash_are_expected_not_group_identity(self):
        rows = [sample("native", 100, "n", image_sha256="native-image"),
                sample("xhyper", 110, "x", image_sha256="xhyper-image")]
        groups = summarize(rows)["groups"]
        self.assertEqual(len(groups), 1)
        self.assertAlmostEqual(groups[0]["delta_pct"], 10)

    def test_failures_and_missing_values_are_retained(self):
        rows = [sample("native", 100, "n-good"), sample("native", 1000, "n-bad", valid=False),
                sample("xhyper", None, "x-missing", exclusion_reasons=["upload_failed"])]
        output = summarize(rows)
        group = output["groups"][0]
        self.assertEqual(len(output["rows"]), 3)
        self.assertEqual(group["native"]["mean"], 100)
        self.assertEqual(group["native"]["n_excluded"], 1)
        self.assertIsNone(group["xhyper"]["mean"])
        self.assertIsNone(group["delta_pct"])
        self.assertIn("upload_failed", output["rows"][2]["exclusion_reasons"])

    def test_duplicate_run_ids_and_uuid_exclude_all_duplicates(self):
        rows = [sample("native", 100, "reused"), sample("xhyper", 200, "reused"),
                sample("native", 90, "n2", uuid="same-doc"),
                sample("native", 80, "n3", uuid="same-doc")]
        output = summarize(rows)
        self.assertEqual(output["groups"][0]["native"]["n_valid"], 0)
        self.assertEqual(len(output["rows"]), 4)
        self.assertIn("duplicate_run_id", output["rows"][0]["exclusion_reasons"])
        self.assertIn("duplicate_uuid", output["rows"][2]["exclusion_reasons"])

    def test_same_run_can_provide_distinct_metrics(self):
        rows = [sample("native", 100, "n", uuid="doc", metric="cpu.single"),
                sample("native", 200, "n", uuid="doc", metric="cpu.multi")]
        output = summarize(rows)
        self.assertEqual(len(output["groups"]), 2)
        self.assertTrue(all(row["accepted"] for row in output["rows"]))

    def test_nonfinite_boolean_and_fake_valid_are_not_scores(self):
        rows = [sample("native", float("nan"), "n1"), sample("native", float("inf"), "n2"),
                sample("native", True, "n3"), sample("native", 100, "n4", valid="false")]
        output = summarize(rows)
        self.assertEqual(output["groups"][0]["native"]["n_valid"], 0)
        self.assertTrue(all(not row["accepted"] for row in output["rows"]))

    def test_zero_baseline_is_undefined_not_infinite(self):
        output = summarize([sample("native", 0, "n"), sample("xhyper", 2, "x")])
        group = output["groups"][0]
        self.assertIsNone(group["delta_pct"])
        self.assertEqual(group["delta_reason"], "native_mean_zero")
        self.assertEqual(group["native"]["n_valid"], 1)
        self.assertIsNone(group["native"]["sample_sd"])

    def test_missing_boot_id_bad_identity_and_unknown_mode_are_visible(self):
        rows = [sample("native", 100, "n1", boot_id=None),
                sample("native", 100, "n2", comparison_key=object()),
                sample("other", 100, "n3"), "not a row"]
        output = summarize(rows)
        self.assertEqual(len(output["rows"]), 4)
        self.assertTrue(all(not row["accepted"] for row in output["rows"]))
        self.assertIn("invalid_comparison_key", output["rows"][1]["exclusion_reasons"])

    def test_empty_data_is_explicitly_empty(self):
        self.assertEqual(summarize([]), {"groups": [], "rows": [], "issues": []})


if __name__ == "__main__":
    unittest.main()
