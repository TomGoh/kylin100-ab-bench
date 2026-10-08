"""Publish an offline Chinese report using the existing comparison statistics."""

import json
import math
import shutil
import tempfile
from pathlib import Path

from .compare import summarize


_MODES = {"native": "原生 Android", "xhyper": "搭载 XHyper"}
_SOURCES = (
    "source", "sources", "source_file", "source_path", "source_trace", "source_csv",
    "source_csv_sha256", "result_path", "provenance", "image_sha256", "manifest_path",
)
_PRIMARY_METRICS = {
    "warm_reboot_to_system_complete_s": "正常重启至系统完成（观测上界）",
    "geekbench_cpu_single": "Geekbench CPU 单核",
    "geekbench_cpu_multi": "Geekbench CPU 多核",
    "geekbench_gpu_score": "Geekbench GPU",
    "geekbench_cpu_internal_runtime_s": "CPU 内部运行时长",
    "geekbench_gpu_internal_runtime_s": "GPU 内部运行时长",
    "memory_baseline_visible_total_bytes": "系统可见内存总量",
    "memory_baseline_available_bytes": "基线可用内存",
    "memory_baseline_estimated_unavailable_bytes": "基线估算非可用内存",
    "geekbench_cpu_mem_available_sampled_min_bytes": "CPU 运行中采样可用内存最低值",
    "geekbench_gpu_mem_available_sampled_min_bytes": "GPU 运行中采样可用内存最低值",
    "geekbench_cpu_memory_after_available_bytes": "CPU 恢复后可用内存",
    "geekbench_gpu_memory_after_available_bytes": "GPU 恢复后可用内存",
}


def _overview(groups):
    rows = []
    for group in groups:
        metric, identity = group["metric"], group["comparison_key"]
        label = _PRIMARY_METRICS.get(metric)
        if metric.endswith(("_mean_power_w", "_energy_j")):
            suffix = "_mean_power_w" if metric.endswith("_mean_power_w") else "_energy_j"
            workload = metric[:-len(suffix)]
            label = {"screen_on_idle": "亮屏静置", "screen_off_idle": "熄屏静置",
                     "screen_off_standby": "已验证休眠窗口", "geekbench_cpu": "CPU 运行窗口",
                     "geekbench_gpu": "GPU 运行窗口"}.get(workload, workload)
            label += "平均功率" if suffix == "_mean_power_w" else "能量"
        if label is None:
            continue
        if identity.get("power_boundary") == "battery_net":
            label += "（电池净变化）"
        if identity.get("api"):
            label += " / " + str(identity["api"])
        unit = identity.get("unit", "")
        scale = 1024 ** 2 if unit == "bytes" else 1
        unit = "MiB" if unit == "bytes" else unit
        means = [_number(group[mode]["mean"] / scale if group[mode]["mean"] is not None else None)
                 for mode in ("native", "xhyper")]
        delta = _number(group["delta_pct"]) + "%" if group["delta_pct"] is not None else "—"
        cells = [label, unit, means[0], means[1], delta,
                 str(group["native"]["n_valid"]) + " / " + str(group["xhyper"]["n_valid"])]
        rows.append("| " + " | ".join(_cell(value) for value in cells) + " |")
    if not rows:
        return []
    return ["## 核心指标总览", "", "每一行仍采用下文对应分组的身份与观测窗口。有效次数按完整运行计数；缺失值显示为“—”。内存总览换算为 MiB，原始字节值保留在完整数据中。", "",
            "| 指标 | 单位 | 原生均值 | XHyper 均值 | 差异 | 有效次数 原生 / XHyper |",
            "|---|---|---:|---:|---:|---:|", *rows, ""]


def _json_safe(value, notes, path="$"):
    """Keep invalid originals visible without emitting nonstandard JSON NaN."""
    if isinstance(value, float) and not math.isfinite(value):
        notes.append({"path": path, "original_repr": repr(value), "reason": "nonfinite_value"})
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                notes.append({"path": path, "original_repr": repr(key), "reason": "nonstring_object_key"})
            result[str(key)] = _json_safe(item, notes, path + "." + str(key))
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, notes, f"{path}[{index}]") for index, item in enumerate(value)]
    notes.append({"path": path, "original_repr": repr(value), "reason": "non_json_original"})
    return {"original_type": type(value).__name__, "original_repr": repr(value)}


def _cell(value):
    if value is None:
        return "未提供"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace("|", "&#124;").replace("\r", " ").replace("\n", "<br>")


def _number(value, missing="—"):
    if value is None:
        return missing
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format(value, ".6g")
    return _cell(value)


def _mode(value):
    return _MODES.get(value, value) if isinstance(value, str) else value


def _unavailable(group):
    missing = [_MODES[mode] + "侧缺少有效数据" for mode in _MODES
               if group[mode]["n_valid"] == 0]
    if missing:
        return "；".join(missing)
    if group["delta_reason"] == "native_mean_zero":
        return "原生均值为零，百分比没有定义"
    return "差异百分比不可用：" + str(group["delta_reason"] or "缺少有限数值")


def _power_boundary(boundary):
    if boundary == "battery_net":
        return "电池净变化；该组结果不能作为整机功耗或续航结论"
    if boundary == "battery_side_device":
        return "电池侧设备边界；有效性依赖执行方提供的完整输入隔离与计量证据"
    return "执行方未提供已识别的电池供电边界"


def _markdown(comparison, campaign, metadata):
    validation = metadata["validation_only"]
    lines = ["# 工具验证报告" if validation else "# 原生 Android 与 XHyper 数据汇总", ""]
    if validation:
        lines += ["**本报告仅用于验证测试工具和数据处理链，不能作为正式原生／XHyper 对比测试结果。**", ""]
    else:
        lines += ["本报告汇总执行方提交的记录。模式、镜像身份和测量条件需要由原始证据支持，报告工具不会自行确认这些身份。", ""]
    lines += _overview(comparison["groups"])
    lines += [
        "差异百分比采用 `(XHyper 均值 / 原生均值 - 1) × 100%`。分数的正值表示提高，时间和设备能耗的正值表示增加。电池净变化需要另外结合充放电方向解释。",
        "", "完整测试运行构成统计样本。报告同时列出按启动标识统计的独立开机数；连续采样点的数量不能替代完整运行次数。本报告没有生成置信区间，也不能仅凭这些差值确认变化由虚拟化层独立造成。",
        "", "Markdown 数字使用最多六位有效数字展示；完整有效原始值保留在 `comparison.json` 中。展示位数不表示传感器精度。表中的“—”表示该统计值不可用，其原因保留在分组和失败记录中。",
        "", "## 执行材料", "",
    ]
    if campaign:
        lines += ["执行方提供了以下测试活动元数据。缺少的截止时间、镜像身份和环境材料没有被补造。", "",
                  "```json", json.dumps(campaign, ensure_ascii=False, indent=2, allow_nan=False), "```", ""]
    else:
        lines += ["执行方没有提供测试活动元数据，因此本报告不能确认截止时间、正式测试用途或镜像交付材料。", ""]
    lines += ["## 分组结果", ""]
    if not comparison["groups"]:
        lines += ["输入没有形成可汇总的指标分组。缺失项目与失败原因仍在后面的记录及 `comparison.json` 中保留。", ""]
    for index, group in enumerate(comparison["groups"], 1):
        identity = group["comparison_key"]
        lines += [f"### 分组 {index}：{_cell(group['metric'])}", "",
                  "本组采用以下工作负载与计量身份。不同身份的记录没有合并。", "",
                  "```json", json.dumps(identity, ensure_ascii=False, indent=2, allow_nan=False), "```", ""]
        if identity.get("power_boundary") is not None:
            lines += ["本组的供电解释为：" + _power_boundary(identity["power_boundary"]) + "。", ""]
        lines += ["| 模式 | 有效运行数 | 独立开机数 | 全部有效值 | 均值 | 中位数 | 样本标准差 | 最小值 | 最大值 | 范围 |",
                  "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|"]
        for mode, label in _MODES.items():
            stats = group[mode]
            values = ", ".join(_number(value) for value in stats["values"]) or "无有效数据"
            fields = [label, stats["n_valid"], stats["distinct_boot_count"], values,
                      _number(stats["mean"]), _number(stats["median"]),
                      _number(stats["sample_sd"], "次数不足"), _number(stats["min"]),
                      _number(stats["max"]), _number(stats["range"])]
            lines.append("| " + " | ".join(_cell(field) for field in fields) + " |")
        if group["delta_pct"] is None:
            lines += ["", "本组没有可用的差异百分比，原因是" + _unavailable(group) + "。", ""]
        else:
            lines += ["", f"本组观察到的差异为 {_number(group['delta_pct'])}%。这一数值描述所提交的样本，重复波动、时间窗口和环境条件仍需要结合原始记录解释。", ""]

    lines += ["## 运行与来源", "",
              "| 行号 | 模式 | 指标 | 运行标识 | 启动标识 | 是否纳入 | 来源材料 |",
              "|---:|---|---|---|---|---|---|"]
    for row in comparison["rows"]:
        sources = {field: row[field] for field in _SOURCES if field in row}
        cells = [row["row_index"], _mode(row.get("mode")), row.get("metric"),
                 row.get("run_id"), row.get("boot_id"), "是" if row["accepted"] else "否",
                 sources or "执行方未提供来源材料"]
        lines.append("| " + " | ".join(_cell(value) for value in cells) + " |")
    if not comparison["rows"]:
        lines += ["", "执行方没有提交指标行。"]
    lines += ["", "## 失败、跳过与缺项", ""]
    excluded = [row for row in comparison["rows"] if not row["accepted"]]
    if excluded:
        for row in excluded:
            original_reason = {key: row[key] for key in ("reason", "error", "status", "skip_reason") if key in row}
            lines.append(f"- 第 {row['row_index']} 行没有纳入统计。排除原因是 {_cell(row['exclusion_reasons'])}；执行方的原始说明为 {_cell(original_reason or None)}。")
        lines.append("")
    else:
        lines += ["所提交指标行的统计检查没有列出排除项；这不代表未提交的测试均已完成。", ""]
    for key, label in (("failed_runs", "失败运行"), ("skipped", "跳过或缺失项目")):
        if key not in campaign:
            lines += ["执行方没有提供" + label + "清单。", ""]
        else:
            entries = campaign[key]
            if isinstance(entries, list):
                lines.append(f"执行方提供的{label}清单包含 {len(entries)} 项。")
                lines += ["", *["- " + _cell(entry) for entry in entries], ""]
            else:
                lines += ["执行方提供的" + label + "原始说明为：" + _cell(entries) + "。", ""]
    if metadata["serialization_notes"]:
        lines += ["非有限数值或不能直接写入标准 JSON 的原始对象保留了表示和位置说明；它们没有被补成零。说明保存在 `comparison.json` 的 `report_metadata.serialization_notes` 中。", ""]
    lines += ["## 可比较数据状态", "",
              "当前输入具有可计算差异百分比的双侧分组。" if metadata["has_comparable_groups"] else
              "当前输入没有可计算差异百分比的双侧分组。单侧数据、空输入和失败记录已经保留，工具没有补造另一侧结果。", ""]
    return "\n".join(lines)


def create_report(metric_rows, directory, *, campaign=None):
    """Create a new report directory and return paths plus comparison status.

    Reuses compare.summarize without recomputing statistics. Publish both files
    by renaming a private sibling staging directory; an existing destination is
    never intentionally reused. The caller owns this unique destination.
    Invalid raw values become JSON null only with explicit original-value notes.
    campaign.purpose='validation' prominently labels the entire report as tool
    validation even when both modes contain numeric data.
    """
    if campaign is not None and not isinstance(campaign, dict):
        raise ValueError("campaign must be a mapping or None")
    rows = list(metric_rows)
    summary = summarize(rows)
    notes = []
    comparison = _json_safe(summary, notes)
    safe_campaign = _json_safe(campaign or {}, notes, "$.campaign")
    validation = safe_campaign.get("purpose") == "validation" or any(
        isinstance(row, dict) and (row.get("validation_only") is True or row.get("purpose") == "validation")
        for row in rows)
    if validation:
        safe_campaign["purpose"] = "validation"
    comparable = any(group["delta_pct"] is not None for group in comparison["groups"])
    metadata = {
        "purpose": safe_campaign.get("purpose"),
        "validation_only": validation,
        "has_comparable_groups": comparable, "campaign": safe_campaign,
        "serialization_notes": notes,
        "statistics_source": "abbench.compare.summarize",
        "identity_verified_by_report": False,
    }
    comparison["report_metadata"] = metadata
    markdown = _markdown(comparison, safe_campaign, metadata)
    target = Path(directory).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="." + target.name + "-", dir=target.parent))
    try:
        (staging / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (staging / "report.md").write_text(markdown, encoding="utf-8")
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "created": True, "directory": str(target),
        "comparison_path": str(target / "comparison.json"), "report_path": str(target / "report.md"),
        "has_comparable_groups": comparable, "validation_only": metadata["validation_only"],
        "group_count": len(comparison["groups"]), "input_row_count": len(rows),
    }
