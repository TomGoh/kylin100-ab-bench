"""Small CLI over capability snapshots and offline analysis."""
import argparse
import csv
import json
import subprocess
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
    p = sub.add_parser("supply", help="只读核对物理设备及全部外部供电状态；不自动确认传感器精度")
    p.add_argument("--serial", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("boot", help="观察新启动；只有显式 --reboot 才请求设备重启")
    p.add_argument("--serial", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--reboot", action="store_true")
    p = sub.add_parser("capture-start", help="启动一个有时长上限的设备本地 Perfetto 会话")
    p.add_argument("--serial", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=int, default=900)
    p.add_argument("--mode", choices=("native", "xhyper", "unknown"), default="unknown")
    for command in ("capture-stop", "mark"):
        p = sub.add_parser(command)
        p.add_argument("--run-dir", required=True)
        if command == "mark":
            p.add_argument("--event", required=True)
    p = sub.add_parser("idle", help="自动测量亮屏静置或熄屏待机并恢复屏幕策略")
    p.add_argument("--serial", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--kind", choices=("screen_on_idle", "screen_off_standby"), required=True)
    p.add_argument("--duration", type=int, required=True)
    p.add_argument("--mode", choices=("native", "xhyper"), required=True)
    p = sub.add_parser("geekbench", help="自动运行 CPU 或 GPU，导出本次新结果")
    p.add_argument("--serial", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--kind", choices=("cpu", "gpu"), required=True)
    p.add_argument("--mode", choices=("native", "xhyper"), required=True)
    p.add_argument("--api", choices=("Vulkan", "OpenCL"), default="Vulkan")
    p.add_argument("--expected-boot-id")
    p = sub.add_parser("analyze-capture", help="分析本次导出的单启动 Perfetto 及设备单位证据")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--processor", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    for command in ("suite", "campaign"):
        p = sub.add_parser(command, help="串行自动测试；campaign 调用镜像负责方交付的切换适配器")
        p.add_argument("--serial", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--processor", required=True)
        p.add_argument("--profile", required=True)
        p.add_argument("--manifest")
        p.add_argument("--options", help="测试时长、截止时间等 JSON 配置")
        p.add_argument("--validation-only", action="store_true")
        if command == "suite":
            p.add_argument("--mode", choices=("native", "xhyper"), required=True)
            p.add_argument("--reboot", action="store_true")
        else:
            p.add_argument("--adapter", required=True, help="JSON argv 数组文件；由镜像负责方提供可执行程序")
            p.add_argument("--repetitions", type=int, default=1)
    p = sub.add_parser("report", help="由指标行生成中文报告；验证数据必须加 --validation-only")
    p.add_argument("--input", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--campaign")
    p.add_argument("--validation-only", action="store_true")
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
    p = sub.add_parser("perfetto-export", help="调用已有官方 Trace Processor 导出原始计数器")
    p.add_argument("--trace", required=True)
    p.add_argument("--processor", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("pull-root", help="使用现有 su/tar 导出 Root 生成的测试文件")
    p.add_argument("--serial", required=True)
    p.add_argument("--remote-dir", required=True)
    p.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            from .doctor import snapshot
            if not 0 < args.timeout <= 30:
                raise ValueError("timeout must be >0 and <=30 seconds")
            result = snapshot(args.serial, args.out, args.timeout)
            emit(result)
            return 0 if result["acquisition_complete"] else 2
        if args.command == "supply":
            from .supply import read_supply, verify_supply_snapshot
            if Path(args.out).exists():
                raise ValueError("supply output already exists; choose a new evidence file")
            profile = read_json(args.profile)
            expected = profile.get("serial")
            if not isinstance(expected, str) or not expected.strip():
                raise ValueError("profile must identify the physical serial")
            evidence = read_supply(args.serial)
            qualification = verify_supply_snapshot(evidence, physical_serial=expected, profile=profile)
            result = {"valid": qualification.get("verified_off") is True,
                      "physical_serial_expected": expected, "transport_serial": args.serial,
                      "supply": evidence, "qualification": qualification,
                      "sensor_calibrated": False,
                      "note": "仅核对本次观察时的外部供电；计量窗口须实际保持拔线并核对前后证据。"}
            emit(result, args.out)
            return 0 if result["valid"] else 2
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
        elif args.command == "perfetto-export":
            from .perfetto import export_trace
            result = export_trace(args.trace, args.processor, args.out)
            emit(result)
            return 0
        elif args.command == "pull-root":
            from .adb_io import pull_root
            result = pull_root(args.serial, args.remote_dir, args.out)
            emit(result)
            return 0
        elif args.command == "boot":
            from .boot import measure_boot
            if args.reboot:
                print("即将请求平板重启，并观察新的启动标识。", file=sys.stderr, flush=True)
            result = measure_boot(args.serial, args.out, args.timeout, args.reboot)
        elif args.command == "capture-start":
            from .capture import start_capture
            result = start_capture(args.serial, args.out, args.duration, args.mode)
            emit(result)
            return 0
        elif args.command == "capture-stop":
            from .capture import stop_capture
            result = stop_capture(args.run_dir)
            emit(result)
            return 0
        elif args.command == "mark":
            from .capture import mark_event
            result = mark_event(args.run_dir, args.event)
            emit(result)
            return 0
        elif args.command == "idle":
            from .idle import run_idle
            result = run_idle(args.serial, args.out, kind=args.kind, duration_s=args.duration, mode=args.mode, prepare_keyguard=True)
            emit(result)
            return 0 if result.get("valid") is True else 2
        elif args.command == "geekbench":
            from .geekbench_runner import run_geekbench
            result = run_geekbench(args.serial, args.kind, args.out, mode=args.mode,
                                   api=args.api, expected_boot_id=args.expected_boot_id)
            emit(result)
            return 0 if result.get("valid") is True else 2
        elif args.command == "analyze-capture":
            from .analysis import analyze_capture
            result = analyze_capture(args.run_dir, args.processor, profile=read_json(args.profile),
                                     start_s=args.start, end_s=args.end)
            emit(result)
            return 0 if result.get("valid") is True else 2
        elif args.command in ("suite", "campaign"):
            from .suite import run_suite, run_campaign
            options = read_json(args.options) if args.options else {}
            manifest = read_json(args.manifest) if args.manifest else None
            profile = read_json(args.profile)
            if args.command == "suite":
                if args.reboot:
                    options["reboot"] = True
                result = run_suite(args.serial, args.out, args.mode, args.processor, profile,
                                   validation_only=args.validation_only, manifest=manifest, options=options)
            else:
                result = run_campaign(args.serial, args.out, args.processor, profile,
                                      manifest=manifest, adapter=read_json(args.adapter),
                                      repetitions=args.repetitions, validation_only=args.validation_only, options=options)
            emit(result)
            return 0 if result.get("valid") is True else 2
        elif args.command == "report":
            from .report import create_report
            campaign = read_json(args.campaign) if args.campaign else {}
            if args.validation_only:
                campaign["purpose"] = "validation"
            result = create_report(read_json(args.input), args.out, campaign=campaign)
            emit(result)
            return 0
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
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        emit({"valid": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
