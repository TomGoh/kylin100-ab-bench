"""Check report fidelity, failure retention and measurement wording."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.compare import summarize
from abbench.report import create_report


def row(mode, value, run, boot, **extra):
    return {
        "mode": mode, "value": value, "run_id": run, "boot_id": boot,
        "metric": "cpu.single", "valid": True, "unit": "score", "app_version": "6.7.1",
        "source_file": "runs/" + run + "/result.json", **extra,
    }


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name) / "report"

    def report(self, rows, campaign=None):
        result = create_report(rows, self.target, campaign=campaign)
        return result, json.loads((self.target / "comparison.json").read_text()), (self.target / "report.md").read_text()

    def test_known_values_reuse_statistics_and_keep_sources(self):
        rows = [row("native", 100, "n1", "nb1"), row("native", 200, "n2", "nb2"),
                row("xhyper", 180, "x1", "xb1"), row("xhyper", 240, "x2", "xb1")]
        originals = copy.deepcopy(rows)
        expected = summarize(rows)
        result, comparison, markdown = self.report(rows)
        self.assertEqual(rows, originals)
        self.assertEqual(comparison["groups"], expected["groups"])
        self.assertEqual(comparison["rows"], expected["rows"])
        self.assertTrue(result["has_comparable_groups"])
        self.assertEqual(comparison["groups"][0]["native"]["distinct_boot_count"], 2)
        self.assertEqual(comparison["groups"][0]["xhyper"]["distinct_boot_count"], 1)
        self.assertIn("100, 200", markdown)
        self.assertIn("180, 240", markdown)
        self.assertIn("40%", markdown)
        self.assertIn("runs/n1/result.json", markdown)
        self.assertIn("样本标准差", markdown)
        self.assertIn("不能仅凭这些差值确认变化由虚拟化层独立造成", markdown)
        self.assertIsNone(comparison["groups"][0]["confidence_interval"])

    def test_validation_purpose_labels_whole_report_even_with_two_modes(self):
        result, comparison, markdown = self.report(
            [row("native", 10, "n", "nb"), row("xhyper", 11, "x", "xb")],
            campaign={"purpose": "validation", "serial": "test-device"})
        self.assertTrue(result["validation_only"])
        self.assertTrue(result["has_comparable_groups"])
        self.assertTrue(markdown.startswith("# 工具验证报告\n"))
        self.assertIn("不能作为正式原厂／XHyper 对比测试结果", markdown)
        self.assertFalse(comparison["report_metadata"]["identity_verified_by_report"])

    def test_row_validation_marker_cannot_be_overridden_by_formal_campaign(self):
        for marker in ({"validation_only": True}, {"purpose": "validation"}):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / 'report'
                result = create_report([row('xhyper', 100, 'test', 'boot', **marker)], target,
                                       campaign={'purpose': 'formal'})
                self.assertTrue(result['validation_only'])
                self.assertTrue((target / 'report.md').read_text().startswith('# 工具验证报告'))

    def test_empty_and_single_side_keep_missing_result(self):
        for rows in ([], [row("native", 100, "n", "nb")]):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "report"
                result = create_report(rows, output)
                comparison = json.loads((output / "comparison.json").read_text())
                markdown = (output / "report.md").read_text()
                self.assertFalse(result["has_comparable_groups"])
                self.assertIn("没有可计算差异百分比的双侧分组", markdown)
                if rows:
                    group = comparison["groups"][0]
                    self.assertIsNone(group["xhyper"]["mean"])
                    self.assertIsNone(group["delta_pct"])
                    self.assertIn("搭载 XHyper侧缺少有效数据", markdown)

    def test_failures_skips_missing_identity_and_nonfinite_values_are_retained(self):
        rows = [row("native", float("nan"), "n", "nb", reason="传感器未更新"),
                row("xhyper", 15, "x", None, valid=False, reason="模式身份缺失")]
        result, comparison, markdown = self.report(rows, {
            "failed_runs": [{"run_id": "gpu1", "reason": "结果导出失败"}],
            "skipped": [{"metric": "standby", "reason": "累计量未验证"}],
        })
        self.assertFalse(result["has_comparable_groups"])
        self.assertEqual(len(comparison["rows"]), 2)
        self.assertIsNone(comparison["rows"][0]["value"])
        self.assertTrue(comparison["report_metadata"]["serialization_notes"])
        for text in ("传感器未更新", "模式身份缺失", "结果导出失败", "累计量未验证"):
            self.assertIn(text, markdown)
        self.assertIn("missing_or_invalid_boot_id", markdown)
        self.assertIsNone(comparison["groups"][0]["native"]["mean"])
        self.assertNotIn(": NaN", (self.target / "comparison.json").read_text())

    def test_net_battery_data_cannot_be_named_whole_device_power(self):
        rows = [row("native", -0.1, "n", "nb", metric="power.mean", unit="W", power_boundary="battery_net"),
                row("xhyper", -0.2, "x", "xb", metric="power.mean", unit="W", power_boundary="battery_net")]
        _, comparison, markdown = self.report(rows)
        self.assertIn("电池净变化；该组结果不能作为整机功耗或续航结论", markdown)
        self.assertEqual(comparison["groups"][0]["native"]["values"], [-0.1])
        self.assertIn("结合充放电方向解释", markdown)

    def test_versions_and_boundaries_stay_separate(self):
        rows = [row("native", 1, "n", "nb", power_boundary="battery_net"),
                row("xhyper", 2, "x", "xb", power_boundary="battery_side_device")]
        result, comparison, _ = self.report(rows)
        self.assertEqual(len(comparison["groups"]), 2)
        self.assertFalse(result["has_comparable_groups"])

    def test_zero_native_baseline_does_not_invent_percentage(self):
        result, comparison, markdown = self.report([
            row("native", 0, "n", "nb"), row("xhyper", 1, "x", "xb")])
        self.assertFalse(result["has_comparable_groups"])
        self.assertEqual(comparison["groups"][0]["native"]["values"], [0])
        self.assertIsNone(comparison["groups"][0]["delta_pct"])
        self.assertIn("原厂均值为零，百分比没有定义", markdown)

    def test_non_object_and_bad_mode_rows_do_not_destroy_report(self):
        _, comparison, markdown = self.report(["missing record", row([], None, "x", None)])
        self.assertEqual(len(comparison["rows"]), 2)
        self.assertIn("row_not_object", markdown)
        self.assertIn("invalid_mode", markdown)

    def test_existing_directory_is_preserved_and_failed_write_never_publishes(self):
        self.target.mkdir()
        sentinel = self.target / "original.txt"
        sentinel.write_text("keep")
        with self.assertRaises(FileExistsError):
            create_report([], self.target)
        self.assertEqual(sentinel.read_text(), "keep")
        destination = self.target.parent / "failed-report"
        original_write = Path.write_text

        def fail_after_json_written(path, *args, **kwargs):
            if path.name == "report.md":
                self.assertTrue((path.parent / "comparison.json").is_file())
                raise OSError("disk full")
            return original_write(path, *args, **kwargs)

        with patch("abbench.report.Path.write_text", new=fail_after_json_written):
            with self.assertRaises(OSError):
                create_report([], destination)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob(".failed-report-*")))


if __name__ == "__main__":
    unittest.main()
