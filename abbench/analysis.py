"""Combine official counter exports, explicit source binding and existing maths."""
import csv
import hashlib
import io
import json
import math
import statistics
import subprocess
from pathlib import Path
from uuid import UUID

from .capture import write_json
from .perfetto import export_trace
from .power import integrate
from .trace_samples import normalize_battery_csv
from .supply import verify_supply_window


def verified_boottime(output):
    rows = list(csv.DictReader(io.StringIO(output)))
    if not rows:
        return False
    try:
        return all(row["clock_name"] == "BOOTTIME" and int(row["ts"]) == int(row["clock_value"])
                   and int(row["ts"]) >= 0 for row in rows)
    except (KeyError, TypeError, ValueError):
        return False


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def memory_counters(rows, start=None, end=None, *, capture_start_s=None, capture_end_s=None):
    """Summarize observed memory samples, never a fabricated full window.

    Clock verification is the analyzer's responsibility. Capture bounds, when
    supplied, apply to every original memory batch before requested clipping.
    Requested bounds and the actual sampled subwindow remain separate.
    """
    for bound in (start, end, capture_start_s, capture_end_s):
        if bound is not None and (not _finite_number(bound) or bound < 0):
            return {"valid": False, "reason": "invalid_memory_window"}
    if (start is not None and end is not None and end <= start) or (
            capture_start_s is not None and capture_end_s is not None and capture_end_s <= capture_start_s):
        return {"valid": False, "reason": "invalid_memory_window"}
    points = {}
    previous_ts = None
    for row in rows:
        try:
            if row["type"] != "meminfo" or row["name"] not in ("MemAvailable", "MemTotal"):
                continue
            if row["unit"] != "bytes":
                return {"valid": False, "reason": "memory_unit_unverified"}
            if type(row["ts_ns"]) not in (str, int) or type(row["value"]) not in (str, int, float):
                return {"valid": False, "reason": "invalid_memory_counter"}
            timestamp, value = int(row["ts_ns"]), float(row["value"])
            t_s = timestamp / 1e9
        except (KeyError, TypeError, ValueError, OverflowError):
            return {"valid": False, "reason": "invalid_memory_counter"}
        if timestamp < 0 or not math.isfinite(t_s) or not math.isfinite(value) or value < 0 or not value.is_integer():
            return {"valid": False, "reason": "invalid_memory_counter"}
        if previous_ts is not None and timestamp < previous_ts:
            return {"valid": False, "reason": "non_increasing_memory_time"}
        previous_ts = timestamp
        if (capture_start_s is not None and t_s < capture_start_s - 0.02) or (
                capture_end_s is not None and t_s > capture_end_s + 0.02):
            return {"valid": False, "reason": "memory_outside_same_boot_capture_bounds"}
        fields = points.setdefault(timestamp, {})
        if row["name"] in fields:
            return {"valid": False, "reason": "duplicate_memory_counter"}
        fields[row["name"]] = value
    if not points or any(set(fields) != {"MemAvailable", "MemTotal"} for fields in points.values()):
        return {"valid": False, "reason": "incomplete_memory_batches"}
    available = [fields["MemAvailable"] for fields in points.values()]
    total = [fields["MemTotal"] for fields in points.values()]
    if any(a > t for a, t in zip(available, total)) or len(set(total)) != 1:
        return {"valid": False, "reason": "inconsistent_memory_total"}
    selected = [(timestamp / 1e9, fields) for timestamp, fields in points.items()
                if (start is None or timestamp / 1e9 >= start) and (end is None or timestamp / 1e9 <= end)]
    if not selected:
        return {"valid": False, "reason": "no_memory_samples_in_requested_window"}
    times = [time for time, _ in selected]
    gaps = [right - left for left, right in zip(times, times[1:])]
    if any(gap <= 0 for gap in gaps):
        return {"valid": False, "reason": "unrepresentable_memory_time"}
    available = [fields["MemAvailable"] for _, fields in selected]
    return {"valid": True, "reason": None, "mem_total_bytes": total[0],
            "mem_available_mean_bytes": statistics.fmean(available),
            "mem_available_sampled_min_bytes": min(available),
            "estimated_unavailable_mean_bytes": statistics.fmean(total[0] - a for a in available),
            "sample_count": len(selected), "samples_independent": False,
            "actual_sampled_window_start_s": times[0], "actual_sampled_window_end_s": times[-1],
            "actual_sampled_duration_s": times[-1] - times[0],
            "actual_sample_gap_max_s": max(gaps) if gaps else None,
            "actual_sample_gap_mean_s": statistics.fmean(gaps) if gaps else None,
            "requested_window_start_s": start, "requested_window_end_s": end,
            "requested_edges_sampled": (start in times and end in times) if start is not None and end is not None else None,
            "coverage_note": "statistics_use_observed_samples_only_not_complete_requested_window",
            "measurement_boundary": "android_kernel_visible_memory_sampled_subwindow",
            "minimum_note": "sample_minimum_not_true_instantaneous_minimum"}


def _profile_reason(profile, serial):
    if not isinstance(profile, dict) or profile.get("units_validated") is not True:
        return "source_units_unverified"
    if not isinstance(profile.get("serial"), str) or not profile["serial"].strip() or profile["serial"] != serial:
        return "source_profile_serial_mismatch"
    if any(profile.get(field) != expected for field, expected in (
            ("current_unit", "uA"), ("voltage_unit", "uV"), ("charge_unit", "uAh"))):
        return "source_profile_units_missing_or_mismatched"
    for field in ("units_validation_reference", "current_sign_reference"):
        if not isinstance(profile.get(field), str) or not profile[field].strip():
            return "missing_" + field
    if profile.get("current_sign_validated") is not True:
        return "current_sign_unverified"
    if type(profile.get("discharge_sign_candidate")) is not int or profile["discharge_sign_candidate"] not in (-1, 1):
        return "invalid_current_sign"
    return None


def analyze_capture(directory, processor, *, profile=None, start_s=None, end_s=None):
    """Analyze one exported capture; default power stays battery net only.

    Unit validation belongs to the selected device profile. Trace clocks and
    sample coverage must independently match this capture's before/after boot.
    This function never infers an isolated supply or calibrates a fuel gauge.
    The outer valid flag describes completed analysis, not valid measurements;
    callers must inspect memory.valid and power.valid independently.
    """
    target = Path(directory)
    capture = json.loads((target / "capture.json").read_text())
    try:
        before, after = capture["before"], capture["after"]
        transport_serial = capture["serial"]
        serial = capture.get("physical_serial", transport_serial)
        same_boot = before["boot_id"] == after["boot_id"]
        UUID(before["boot_id"])
        bounds = before["t_s"], after["t_s"]
        if (capture["state"] != "exported" or not same_boot or not isinstance(serial, str) or not serial.strip()
                or any(not _finite_number(value) or value < 0 for value in bounds) or bounds[1] <= bounds[0]
                or any(probe.get("clock") != "proc_uptime_boottime" for probe in (before, after))
                or any("physical_serial" in probe and probe["physical_serial"] != serial for probe in (before, after))):
            raise ValueError("invalid capture source binding")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("analysis requires a complete exported single-boot source binding") from exc
    for value in (start_s, end_s):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError("invalid analysis window")
    if start_s is not None and end_s is not None and end_s <= start_s:
        raise ValueError("invalid analysis window")
    out = target / "analysis"
    out.mkdir(exist_ok=False)
    export = export_trace(capture["trace"], processor, out / "trace-export")
    csv_path = out / "trace-export/counter-samples.csv"
    sql = Path(__file__).resolve().parents[1] / "sql/clock-bindings.sql"
    clock = subprocess.run([str(Path(processor).resolve()), capture["trace"], "-q", str(sql)],
                           text=True, capture_output=True, timeout=30, check=True)
    (out / "clock-bindings.csv").write_text(clock.stdout)
    (out / "clock-bindings.stderr.txt").write_text(clock.stderr)
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    clock_ok = verified_boottime(clock.stdout)
    result = {"valid": True, "boot_id": before["boot_id"], "serial": serial,
              "transport_serial": transport_serial, "physical_serial": capture.get("physical_serial"),
              "export": export, "measurement_validated": False,
              "memory": memory_counters(rows, start_s, end_s, capture_start_s=bounds[0], capture_end_s=bounds[1])
              if clock_ok else {"valid": False, "reason": "memory_clock_unverified"},
              "power": {"valid": False, "reason": "source_units_or_clock_unverified"}}
    csv_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    result["source_capture"] = str((target / "capture.json").resolve())
    result["source_csv_sha256"] = csv_sha256
    result["memory"].update(boot_id=before["boot_id"], source_csv_sha256=csv_sha256,
                            clock_binding_verified=clock_ok, measurement_validated=False)
    profile = profile or {}
    result["source_profile"] = profile
    result["clock_binding_verified"] = clock_ok
    profile_reason = _profile_reason(profile, serial)
    if profile_reason is not None:
        result["power"] = {"valid": False, "reason": profile_reason}
    if profile_reason is None and clock_ok:
        evidence = {"boot_id": result["boot_id"], "csv_sha256": csv_sha256,
                    "clock": "CLOCK_BOOTTIME", "ts_unit": "ns",
                    "units": {"batt.current_ua": profile["current_unit"], "batt.voltage_uv": profile["voltage_unit"],
                              "batt.charge_uah": profile["charge_unit"]},
                    "reference": str(out / "clock-bindings.csv") + "; " + profile["units_validation_reference"]
                    + "; " + profile["current_sign_reference"]}
        normalized = normalize_battery_csv(csv_path, boot_id=result["boot_id"],
                                            source_verified=True, source_evidence=evidence)
        write_json(out / "normalized-battery.json", normalized)
        if normalized["valid"]:
            samples = normalized["samples"]
            coverage_ok = (samples[0]["t_s"] >= capture["before"]["t_s"] - 0.02
                           and samples[-1]["t_s"] <= capture["after"]["t_s"] + 0.02)
            if not coverage_ok:
                result["power"] = {"valid": False, "reason": "trace_outside_same_boot_capture_bounds"}
            elif profile.get("current_sign_validated") is not True:
                result["power"] = {"valid": False, "reason": "current_sign_unverified"}
            else:
                supply = verify_supply_window(capture.get("supply_before"), capture.get("supply_after"),
                    boot_id=result["boot_id"], physical_serial=capture.get("physical_serial"), profile=profile,
                    start_s=min(samples[0]["t_s"], start_s) if start_s is not None else samples[0]["t_s"],
                    end_s=max(samples[-1]["t_s"], end_s) if end_s is not None else samples[-1]["t_s"])
                result["supply_evidence"] = supply
                boundary = "battery_side_device" if supply["verified_off"] else "battery_net"
                if supply["verified_off"]:
                    for sample in samples:
                        sample["external_online"] = False
                    normalized["external_supply_evidence"] = supply
                    write_json(out / "normalized-battery.json", normalized)
                config = {"power_boundary": boundary, "discharge_sign": profile["discharge_sign_candidate"],
                          "max_gap_s": profile.get("max_sample_gap_s", 3), "max_read_span_s": None,
                          "input_supply_verified_off": supply["verified_off"], "window_start_s": start_s, "window_end_s": end_s}
                result["power"] = integrate(samples, config)
                result["power"].update(measurement_validated=False, sensor_calibrated=False,
                                       boundary_note=("同次启动前后观察到所有已报告外部输入关闭，按设备电池侧计量；辅助角色只绑定软件接口分类，不能证明硬件隔离。期间需实际保持线缆拔除且无其他外部输入，端点不是连续监测，传感器未经校准。"
                                                      if supply["verified_off"] else "电池净能量变化；外部供电未证实关闭，不能报告整机功耗。"),
                                       read_span_note="Perfetto批次时间不证明硬件同时读取或零耗时")
        else:
            result["power"] = {"valid": False, "reason": normalized["reason"]}
    write_json(out / "analysis.json", result)
    return result
