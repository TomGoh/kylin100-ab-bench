"""按工作负载身份汇总 A/B 数据；保留失败和重复，不自动推断显著性。"""

from __future__ import annotations

import json
import math
import statistics
from typing import Any, Iterable


IDENTITY_FIELDS = ("version", "app_version", "benchmark_version", "workload_version",
                   "apk_sha256", "api", "api_raw", "tool", "unit", "configuration",
                   "measurement_boundary", "power_boundary", "purpose", "observer")


def _valid_flag(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [row for row in rows if row["accepted"]]
    values = [row["value"] for row in accepted]
    boot_ids = sorted({row["boot_id"] for row in accepted})
    mean = statistics.mean(values) if values else None
    spread = max(values) - min(values) if values else None
    return {"n_total": len(rows), "n_valid": len(values), "n_excluded": len(rows) - len(values),
            "values": values, "run_ids": [row["run_id"] for row in accepted],
            "boot_ids": boot_ids, "distinct_boot_count": len(boot_ids),
            "mean": mean, "median": statistics.median(values) if values else None,
            "sample_sd": statistics.stdev(values) if len(values) > 1 else None,
            "min": min(values) if values else None, "max": max(values) if values else None,
            "range": spread,
            "relative_range_pct": spread / abs(mean) * 100 if mean not in (None, 0) else None}


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """返回 groups、rows 和 issues；不修改传入行，不生成默认置信区间。

    行的必需字段为 mode(native/xhyper)、metric、value、boot_id、run_id、valid。
    comparison_key 可为任意 JSON 值；version/api 等 IDENTITY_FIELDS 也自动参与
    分组，防止误把不同版本、接口或计量边界相加。metric 加 run_id 或 uuid 重复
    的所有行均保留并排除。同一个运行的不同 metric 可以分别保留。

    每组提供两侧原始值、均值、中位数、样本标准差、范围和独立开机数；delta_pct
    定义为 (xhyper_mean/native_mean - 1)*100。缺失或零基线时返回 None 和原因。
    """
    processed: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for index, original in enumerate(rows):
        if not isinstance(original, dict):
            processed.append({"input": original, "row_index": index, "accepted": False,
                              "exclusion_reasons": ["row_not_object"]})
            issues.append({"code": "row_not_object", "row_index": index})
            continue
        row = dict(original)
        reasons = list(row.get("exclusion_reasons", [])) if isinstance(row.get("exclusion_reasons", []), list) else ["invalid_exclusion_reasons"]
        if row.get("mode") not in ("native", "xhyper"):
            reasons.append("invalid_mode")
        for field in ("metric", "boot_id", "run_id"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                reasons.append(f"missing_or_invalid_{field}")
        if not _valid_flag(row.get("valid")):
            reasons.append("invalid_result")
        value = row.get("value")
        if type(value) not in (int, float) or not math.isfinite(value):
            reasons.append("missing_or_nonfinite_value")
        identity = {"comparison_key": row.get("comparison_key"),
                    **{field: row[field] for field in IDENTITY_FIELDS if field in row}}
        try:
            token = json.dumps([row.get("metric"), identity], sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError):
            token = None
            reasons.append("invalid_comparison_key")
        row.update(row_index=index, accepted=not reasons,
                   exclusion_reasons=list(dict.fromkeys(reasons)))
        processed.append(row)
        if token is not None and isinstance(row.get("metric"), str) and row["metric"].strip():
            group = groups.setdefault(token, {"metric": row["metric"],
                                               "comparison_key": identity, "rows": []})
            group["rows"].append(row)

    # An identical metric from one run cannot appear twice, even under different modes or versions.
    for field, code in (("run_id", "duplicate_run_id"), ("uuid", "duplicate_uuid")):
        buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in processed:
            metric, value = row.get("metric"), row.get(field)
            if isinstance(metric, str) and isinstance(value, str) and value.strip():
                buckets.setdefault((metric, value), []).append(row)
        for (metric, value), duplicates in buckets.items():
            if len(duplicates) < 2:
                continue
            issues.append({"code": code, "metric": metric, "value": value,
                           "row_indices": [row["row_index"] for row in duplicates]})
            for row in duplicates:
                row["accepted"] = False
                if code not in row["exclusion_reasons"]:
                    row["exclusion_reasons"].append(code)

    result_groups: list[dict[str, Any]] = []
    for token in sorted(groups):
        group = groups[token]
        for mode in ("native", "xhyper"):
            group[mode] = _stats([row for row in group["rows"] if row.get("mode") == mode])
        native, xhyper = group["native"]["mean"], group["xhyper"]["mean"]
        if native is None or xhyper is None:
            group.update(delta_pct=None, delta_reason="insufficient_valid_samples")
        elif native == 0:
            group.update(delta_pct=None, delta_reason="native_mean_zero")
        else:
            group.update(delta_pct=(xhyper / native - 1) * 100, delta_reason=None)
        group["excluded_rows"] = [row["row_index"] for row in group["rows"] if not row["accepted"]]
        group["confidence_interval"] = None
        group["confidence_interval_reason"] = "not_estimated; repeated readings are not independent experiments"
        result_groups.append(group)

    for row in processed:
        if not row["accepted"]:
            issues.append({"code": "excluded_row", "row_index": row["row_index"],
                           "reasons": row["exclusion_reasons"]})
    return {"groups": result_groups, "rows": processed, "issues": issues}
