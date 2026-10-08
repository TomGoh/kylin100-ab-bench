"""Small CLI over capability snapshots and offline analysis."""
import argparse
import csv
import json
import sys
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def emit(value, path=None):
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(target)
    else:
        sys.stdout.write(encoded)


def load_samples(path):
    if Path(path).suffix == ".json":
        return read_json(path)
    with Path(path).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for field in ("t_s", "read_end_s", "voltage_uv", "current_ua", "charge_uah", "battery_temp_decic"):
            if field in row:
                row[field] = None if row[field] in ("", "NA") else float(row[field])
        raw = row.get("external_online", "")
        if raw in ("true", "1"):
            row["external_online"] = True
        elif raw in ("false", "0"):
            row["external_online"] = False
        elif raw in ("", "NA", None):
            row["external_online"] = None
        else:
            raise ValueError("external_online must be 0/1/true/false/NA")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Android 原生/XHyper 测试参考工具")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor", help="只读能力快照；不自动确认计量能力")
    p.add_argument("--serial", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=float, default=12)
    p = sub.add_parser("validate-manifest", help="核对镜像交付清单")
    p.add_argument("input")
    for name in ("compare", "endpoint"):
        p = sub.add_parser(name)
        p.add_argument("--input", required=True)
        p.add_argument("--out")
    p = sub.add_parser("export-geekbench")
    p.add_argument("--db", required=True)
    p.add_argument("--out")
    p = sub.add_parser("power")
    p.add_argument("--samples", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--out")
    p = sub.add_parser("memory", help="解析轻量内存快照；不测量 Android 不可见保留区")
    p.add_argument("--meminfo", required=True)
    p.add_argument("--zram")
    p.add_argument("--out")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            from .doctor import snapshot
            if not 0 < args.timeout <= 30:
                raise ValueError("timeout must be >0 and <=30 seconds")
            result = snapshot(args.serial, args.out, args.timeout)
            emit(result)
            return 0 if result["acquisition_complete"] else 2
        if args.command == "validate-manifest":
            from .manifest import validate_manifest
            result = validate_manifest(read_json(args.input))
        elif args.command == "export-geekbench":
            from .geekbench import export_database
            result = export_database(args.db)
        elif args.command == "compare":
            from .compare import summarize
            rows = read_json(args.input)
            if not isinstance(rows, list):
                raise ValueError("comparison input must be a JSON row array")
            result = summarize(rows)
            result["has_comparable_groups"] = any(group["delta_reason"] is None for group in result["groups"])
        elif args.command == "power":
            from .power import integrate
            result = integrate(load_samples(args.samples), read_json(args.config))
        elif args.command == "memory":
            from .memory import summarize_snapshot
            result = summarize_snapshot(
                Path(args.meminfo).read_text(encoding="utf-8"),
                Path(args.zram).read_text(encoding="utf-8") if args.zram else None)
        else:
            from .power import endpoint
            data = read_json(args.input)
            result = endpoint(data["start"], data["end"], data["config"])
        emit(result, getattr(args, "out", None))
        if args.command == "compare" and not result["has_comparable_groups"]:
            return 2
        if args.command == "export-geekbench" and result["counts"]["valid"] == 0:
            return 2
        return 2 if result.get("valid") is False else 0
    except (ValueError, KeyError, OSError) as exc:
        emit({"valid": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
