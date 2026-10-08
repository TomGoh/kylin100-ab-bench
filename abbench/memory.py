"""从设备导出的轻量快照计算内存指标，不把可见内存差解释为虚拟化层独占。"""

from __future__ import annotations

import re
from typing import Any


BYTE_FIELDS = {
    "MemTotal": "mem_total_bytes",
    "MemAvailable": "mem_available_bytes",
    "MemFree": "mem_free_bytes",
    "Buffers": "buffers_bytes",
    "Cached": "cached_bytes",
    "Slab": "slab_bytes",
    "SReclaimable": "sreclaimable_bytes",
    "SUnreclaim": "sunreclaim_bytes",
    "KernelStack": "kernel_stack_bytes",
    "PageTables": "page_tables_bytes",
    "CmaTotal": "cma_total_bytes",
    "CmaFree": "cma_free_bytes",
    "SwapTotal": "swap_total_bytes",
    "SwapFree": "swap_free_bytes",
    "Shmem": "shmem_bytes",
}
_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_()]*):\s*([+-]?\d+)(?:\s+(\S+))?\s*$")


def summarize_snapshot(meminfo_text: str, zram_mm_stat_text: str | None = None) -> dict[str, Any]:
    """解析 /proc/meminfo 文本及可选 zram mm_stat 文本，返回独立字节指标。

    Linux meminfo 的 kB 按 1024 字节换算；MemTotal/MemAvailable 必须存在。
    estimated_unavailable_bytes=MemTotal-MemAvailable 是不可立即获得内存的估计，
    不是应用进程占用。所有可选字段缺失返回 None。缓存、Slab 子项、共享内存、
    Swap 逻辑用量和 zram 物理用量互有包含关系，本函数不把它们相加。

    zram 前三个无单位整数按标准 Linux mm_stat 字节字段解释，并始终标记
    schema_verified=False；设备接口及语义仍需执行方验证。空或少于三列会拒绝，
    不能补零。函数不读取设备、不修改缓存，也不设置 mode/boot_id/run_id。
    """
    if not isinstance(meminfo_text, str):
        raise ValueError("meminfo_text 必须是文本")
    raw_fields: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(meminfo_text.splitlines(), 1):
        if not line.strip():
            continue
        match = _LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"meminfo 第 {line_number} 行格式无效")
        name, number, unit = match.groups()
        if name in raw_fields:
            raise ValueError(f"meminfo 字段重复: {name}")
        value = int(number)
        if value < 0:
            raise ValueError(f"meminfo 字段不能为负: {name}")
        if name in BYTE_FIELDS and unit != "kB":
            raise ValueError(f"meminfo 字段 {name} 需要 Linux kB 单位，实际为 {unit!r}")
        raw_fields[name] = {"value": value, "unit": unit}

    for required in ("MemTotal", "MemAvailable"):
        if required not in raw_fields:
            raise ValueError(f"meminfo 缺少必需字段: {required}")
    result: dict[str, Any] = {
        output: raw_fields[source]["value"] * 1024 if source in raw_fields else None
        for source, output in BYTE_FIELDS.items()
    }
    total, available = result["mem_total_bytes"], result["mem_available_bytes"]
    if total <= 0:
        raise ValueError("MemTotal 必须大于零")
    if available > total:
        raise ValueError("MemAvailable 不能大于 MemTotal")
    for total_name, free_name in (("MemTotal", "MemFree"), ("CmaTotal", "CmaFree"), ("SwapTotal", "SwapFree")):
        if total_name in raw_fields and free_name in raw_fields:
            if raw_fields[free_name]["value"] > raw_fields[total_name]["value"]:
                raise ValueError(f"{free_name} 不能大于 {total_name}")

    result["estimated_unavailable_bytes"] = total - available
    result["available_fraction"] = available / total
    swap_total, swap_free = result["swap_total_bytes"], result["swap_free_bytes"]
    result["swap_used_bytes"] = swap_total - swap_free if swap_total is not None and swap_free is not None else None
    result["raw_meminfo_fields"] = raw_fields
    result["missing_optional_fields"] = [name for name in BYTE_FIELDS if name not in raw_fields]
    result["measurement_boundary"] = "android_kernel_visible_memory"
    result["zram"] = None
    if zram_mm_stat_text is not None:
        if not isinstance(zram_mm_stat_text, str):
            raise ValueError("zram_mm_stat_text 必须是文本或 None")
        tokens = zram_mm_stat_text.split()
        if len(tokens) < 3 or any(re.fullmatch(r"\d+", token) is None for token in tokens):
            raise ValueError("zram mm_stat 需要至少三列非负整数；缺失数据不能补零")
        numbers = [int(token) for token in tokens]
        result["zram"] = {"orig_data_size_bytes": numbers[0], "compr_data_size_bytes": numbers[1],
                          "mem_used_total_bytes": numbers[2], "raw_columns": numbers,
                          "schema": "linux_mm_stat_first_three_bytes",
                          "schema_verified": False,
                          "verification_required": "确认当前设备节点采用标准 Linux mm_stat 字段顺序及字节单位"}
    return result
