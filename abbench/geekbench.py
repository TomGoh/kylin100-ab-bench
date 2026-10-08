"""离线导入 Geekbench 安卓历史数据库，保留原始结果和失败原因。"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
from typing import Any


class DatabaseExportError(ValueError):
    """数据库无法可靠读取；调用方不能将该错误转换为零分。"""


def _flag(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def export_database(path: str | Path) -> dict[str, Any]:
    """导出一个一致性数据库快照，不操作设备或修改主数据库。

    返回 source、schema、records、issues 和 counts。每条 record 保留完整
    document/raw_json，kind 取 cpu/gpu/unclassified，api_raw 保留数据库原值。
    metrics 是 single/multi 或 score 字典；valid 是考虑完整性和重复后的有效性，
    app_valid 保留应用的原始标志。损坏数据库或缺失 documents 必需列会抛出
    DatabaseExportError；缺失 CPU/GPU 表只把相应能力标为 unavailable。

    调用前必须取得一致性快照；此函数不能修复运行中只复制主文件造成的缺页。
    """
    source = Path(path).expanduser().resolve()
    try:
        con = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise DatabaseExportError(f"无法打开数据库 {source}: {exc}") from exc

    try:
        integrity = [row[0] for row in con.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise DatabaseExportError(f"数据库完整性检查失败: {integrity}")
        tables = {
            row["name"]: row["sql"]
            for row in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        }
        columns = {
            name: [row["name"] for row in con.execute(f'PRAGMA table_info("{name}")')]
            for name in ("documents", "cpu_documents", "gpu_documents")
            if name in tables
        }
        if not {"id", "json"}.issubset(columns.get("documents", [])):
            raise DatabaseExportError("documents 表缺失或缺少 id/json 必需列")

        issues: list[dict[str, Any]] = []
        references: dict[Any, list[dict[str, Any]]] = {}
        capabilities: dict[str, str] = {}
        required = {
            "cpu": {"document_id", "score", "multicore_score"},
            "gpu": {"document_id", "score", "api"},
        }
        for kind, fields in required.items():
            table = f"{kind}_documents"
            if not fields.issubset(columns.get(table, [])):
                capabilities[kind] = "unavailable"
                issues.append({"code": "schema_unavailable", "kind": kind,
                               "missing_columns": sorted(fields - set(columns.get(table, [])))})
                continue
            capabilities[kind] = "available"
            for row in con.execute(f"SELECT * FROM {table}"):
                references.setdefault(row["document_id"], []).append({"kind": kind, "row": dict(row)})

        records: list[dict[str, Any]] = []
        seen_document_ids: set[Any] = set()
        for row in con.execute("SELECT id, json FROM documents ORDER BY id"):
            did = row["id"]
            seen_document_ids.add(did)
            links = references.get(did, [])
            reasons: list[str] = []
            document: Any = None
            try:
                document = json.loads(row["json"])
                if not isinstance(document, dict):
                    reasons.append("json_not_object")
            except (ValueError, TypeError, UnicodeDecodeError):
                reasons.append("invalid_json")

            link = links[0] if len(links) == 1 else None
            kind = link["kind"] if link else "unclassified"
            if not links:
                reasons.append("missing_kind_reference")
            elif len(links) > 1:
                reasons.append("ambiguous_kind_reference")
            obj = document if isinstance(document, dict) else {}
            app_valid = obj.get("valid")
            complete_raw = obj.get("complete_benchmark")
            if not _flag(app_valid):
                reasons.append("application_invalid" if "valid" in obj else "valid_missing")
            if not _flag(complete_raw):
                reasons.append("benchmark_incomplete" if "complete_benchmark" in obj else "complete_missing")
            if not isinstance(obj.get("version"), str) or not obj["version"].strip():
                reasons.append("version_missing")

            metric_fields = {"single": "score", "multi": "multicore_score"} if kind == "cpu" else (
                {"score": "score"} if kind == "gpu" else {})
            metrics = {name: obj.get(field) for name, field in metric_fields.items()}
            for name, field in metric_fields.items():
                if not _number(metrics[name]):
                    reasons.append(f"invalid_score:{name}")
                elif link["row"].get(field) != metrics[name]:
                    reasons.append(f"score_mismatch:{name}")
            records.append({
                "document_id": did, "kind": kind,
                "api_raw": link["row"].get("api") if link and kind == "gpu" else None,
                "uuid": obj.get("uuid"), "version": obj.get("version"),
                "app_valid": app_valid, "complete_raw": complete_raw,
                "complete": _flag(complete_raw), "valid": not reasons,
                "exclusion_reasons": reasons, "metrics": metrics,
                "document": document, "raw_json": row["json"],
                "references": links,
            })

        for did in references.keys() - seen_document_ids:
            issues.append({"code": "orphan_reference", "document_id": did,
                           "references": references[did]})
            records.append({"document_id": did, "kind": "unclassified", "api_raw": None,
                            "uuid": None, "version": None, "app_valid": None,
                            "complete_raw": None, "complete": False, "valid": False,
                            "exclusion_reasons": ["orphan_reference"], "metrics": {},
                            "document": None, "raw_json": None, "references": references[did]})

        for field, code in (("uuid", "duplicate_uuid"), ("document_id", "duplicate_document_id")):
            buckets: dict[Any, list[dict[str, Any]]] = {}
            for rec in records:
                value = rec[field]
                if value is None or value == "":
                    continue
                if not isinstance(value, (str, int, float, bool)):
                    rec["exclusion_reasons"].append(f"invalid_{field}")
                    rec["valid"] = False
                    continue
                buckets.setdefault(value, []).append(rec)
            for value, duplicates in buckets.items():
                if len(duplicates) < 2:
                    continue
                issues.append({"code": code, "value": value,
                               "document_ids": [rec["document_id"] for rec in duplicates]})
                for rec in duplicates:
                    rec["exclusion_reasons"].append(code)
                    rec["valid"] = False

        return {"source": str(source), "schema": {"tables": tables, "columns": columns,
                                                   "capabilities": capabilities},
                "records": records, "issues": issues,
                "counts": {"total": len(records), "valid": sum(rec["valid"] for rec in records),
                           "invalid": sum(not rec["valid"] for rec in records),
                           "cpu": sum(rec["kind"] == "cpu" for rec in records),
                           "gpu": sum(rec["kind"] == "gpu" for rec in records)}}
    except sqlite3.Error as exc:
        raise DatabaseExportError(f"无法可靠读取数据库 {source}: {exc}") from exc
    finally:
        con.close()
