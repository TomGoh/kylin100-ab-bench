"""Read-only environment and per-boot memory snapshots outside power windows.

Every command is surrounded by device boot identity and uptime reads. Thermal
zone values remain raw because their vendor units have not been established.
Memory snapshot repeats describe one boot baseline, not independent trials.
"""
import json
import math
import re
import shlex
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean

from .memory import summarize_snapshot


SETTINGS = {
    "screen_brightness": "system", "screen_brightness_mode": "system",
    "screen_off_timeout": "system", "stay_on_while_plugged_in": "global",
    "peak_refresh_rate": "system", "min_refresh_rate": "system",
    "wifi_on": "global", "bluetooth_on": "global", "airplane_mode_on": "global",
    "volume_music": "system",
}

ENVIRONMENT_COMMANDS = {
    "fingerprint": "getprop ro.build.fingerprint",
    "kernel-runtime": "sha256sum /sys/kernel/notes; uname -a",
    "battery": "dumpsys battery; printf '\\nAB_CURRENT_CANDIDATE\\n'; cat /sys/class/power_supply/battery/current_now 2>/dev/null || true",
    "thermal": 'for p in /sys/class/thermal/thermal_zone*; do [ -d "$p" ] || continue; printf "\\n%s\\n" "$p"; cat "$p/type" "$p/temp" 2>/dev/null || true; done',
    "cpu-policy": 'cat /sys/devices/system/cpu/online; for p in /sys/devices/system/cpu/cpufreq/policy*; do [ -d "$p" ] || continue; printf "\\n%s\\n" "$p"; cat "$p/scaling_governor" "$p/scaling_min_freq" "$p/scaling_max_freq" 2>/dev/null || true; done',
    "cpuidle-cmdline": 'cat /proc/cmdline; for p in /sys/devices/system/cpu/cpu*/cpuidle/state*; do [ -d "$p" ] || continue; printf "\\n%s\\n" "$p"; cat "$p/name" "$p/disable" 2>/dev/null || true; done',
    "network-audio": "ip addr; ip route; dumpsys audio",
}


def _write(path, value):
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _decode(value):
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


class _Collector:
    def __init__(self, serial, directory, expected_boot_id=None, timeout_s=8,
                 deadline_monotonic_s=None):
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("serial must be nonempty")
        if deadline_monotonic_s is not None and (isinstance(deadline_monotonic_s, bool) or
                not isinstance(deadline_monotonic_s, (int, float)) or not math.isfinite(deadline_monotonic_s)):
            raise ValueError("deadline_monotonic_s must be finite or null")
        self.serial, self.target = serial, Path(directory)
        self.target.mkdir(parents=True, exist_ok=False)
        self.timeout_s, self.boot_id = timeout_s, expected_boot_id
        self.deadline = deadline_monotonic_s
        self.commands, self.index = {}, 0

    def read(self, label, command, *, required=False):
        remaining = self.deadline - time.monotonic() if self.deadline is not None else self.timeout_s
        if remaining <= 0:
            raise ValueError(label + ": deadline_exhausted")
        timeout = min(self.timeout_s, remaining)
        # A failed payload is marked inside the envelope so that we still read
        # the closing identity. Transport failures preserve incomplete raw data.
        script = ("printf 'AB_ENV_BOOT_BEFORE\\n'; cat /proc/sys/kernel/random/boot_id; "
                  "printf 'AB_ENV_TIME_BEFORE\\n'; cat /proc/uptime; "
                  "printf 'AB_ENV_PAYLOAD\\n'; ( " + command +
                  " ); rc=$?; printf '\\nAB_ENV_RC=%s\\n' \"$rc\"; "
                  "printf 'AB_ENV_TIME_AFTER\\n'; cat /proc/uptime; "
                  "printf 'AB_ENV_BOOT_AFTER\\n'; cat /proc/sys/kernel/random/boot_id")
        item = {"available": False, "error": None, "command": command,
                "host_before_s": time.monotonic(), "returncode": None, "timed_out": False}
        stdout, stderr = "", ""
        try:
            proc = subprocess.run(["adb", "-s", self.serial, "shell", "su", "0", "sh", "-c", shlex.quote(script)],
                                  capture_output=True, text=True, timeout=timeout)
            stdout, stderr = _decode(proc.stdout), _decode(proc.stderr)
            item["returncode"] = proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _decode(exc.stdout), _decode(exc.stderr)
            item.update(timed_out=True, error="command_timeout")
        except OSError as exc:
            stderr, item["error"] = str(exc), "transport_unavailable"
        item["host_after_s"] = time.monotonic()
        if self.deadline is not None and item["host_after_s"] >= self.deadline:
            item["error"] = "deadline_exhausted"
        prefix = f"{self.index:03d}-{label}"
        self.index += 1
        item.update(source=prefix + ".stdout.txt", stderr_source=prefix + ".stderr.txt")
        (self.target / item["source"]).write_text(stdout, encoding="utf-8")
        (self.target / item["stderr_source"]).write_text(stderr, encoding="utf-8")
        self.commands[label] = item
        if item["error"] is None and item["returncode"] != 0:
            item["error"] = "command_failed"
        match = re.fullmatch(r"AB_ENV_BOOT_BEFORE\n([^\n]+)\nAB_ENV_TIME_BEFORE\n([^\n]+)\nAB_ENV_PAYLOAD\n(.*?)\nAB_ENV_RC=(\d+)\nAB_ENV_TIME_AFTER\n([^\n]+)\nAB_ENV_BOOT_AFTER\n([^\n]+)\n?", stdout.replace("\r\n", "\n"), re.S)
        payload = None
        if item["error"] is None:
            if not match:
                item["error"] = "identity_or_clock_envelope_missing"
            else:
                before, t0, payload, rc, t1, after = match.groups()
                try:
                    before, after = str(uuid.UUID(before.strip())), str(uuid.UUID(after.strip()))
                    t0, t1 = float(t0.split()[0]), float(t1.split()[0])
                    if not all(math.isfinite(t) and t >= 0 for t in (t0, t1)) or t1 < t0:
                        raise ValueError("invalid_clock")
                except (ValueError, IndexError):
                    item["error"] = "invalid_identity_or_clock"
                else:
                    if before != after or (self.boot_id is not None and before != self.boot_id):
                        item["error"] = "cross_boot_capture"
                    else:
                        self.boot_id = before
                        item.update(boot_id=before, uptime_before_s=t0, uptime_after_s=t1)
                        if rc != "0":
                            item["error"] = "payload_command_failed"
                        else:
                            item["available"] = True
        if item["error"] in ("cross_boot_capture", "invalid_identity_or_clock", "deadline_exhausted"):
            raise ValueError(label + ": " + item["error"])
        if required and not item["available"]:
            raise ValueError(label + ": " + str(item["error"]))
        return payload if item["available"] else None, item


def parse_battery(raw):
    raw = raw or ""
    fields = {}
    for key in ("temperature", "level", "status"):
        matches = re.findall(r"(?m)^\s*" + key + r":\s*(-?\d+)\s*$", raw)
        fields[key] = int(matches[0]) if len(matches) == 1 else None
    temperature = fields["temperature"]
    # This conversion is the Android battery-service reporting convention.
    # It does not calibrate the hardware or assign units to thermal-zone nodes.
    temperature_c = temperature / 10 if temperature is not None else None
    live = not bool(re.search(r"updates\s+stopped|mUpdatesStopped=true", raw, re.I))
    current = re.search(r"AB_CURRENT_CANDIDATE\n(-?\d+)\s*$", raw)
    capacity = fields["level"] if fields["level"] is not None and 0 <= fields["level"] <= 100 else None
    return {"temperature_c": temperature_c, "temperature_raw": temperature,
            "temperature_reporting_unit": "Android battery service 0.1 degrees C",
            "temperature_is_live": live, "hardware_calibrated": False,
            "capacity_percent": capacity, "status": fields["status"],
            "current_ua_candidate": int(current.group(1)) if current else None}


def capture_environment(serial, out, expected_boot_id=None, *, deadline_monotonic_s=None):
    """Save bounded read-only evidence; unknown optional fields stay null."""
    collector = _Collector(serial, out, expected_boot_id, deadline_monotonic_s=deadline_monotonic_s)
    result = {"serial": serial, "created_utc": datetime.now(timezone.utc).isoformat(),
              "valid": False, "reason": None, "boot_id": None, "uptime_s": None,
              "read_only": True, "mode_verified": False, "fingerprint": None,
              "kernel_runtime_sha256": None, "battery_temperature_c": None,
              "battery": None, "settings": {}, "commands": collector.commands,
              "thermal_units_verified": False, "power_measurement_validated": False,
              "apk_identity_source": "geekbench_runner.metadata"}
    try:
        collector.read("initial-identity", "true", required=True)
        for label, command in ENVIRONMENT_COMMANDS.items():
            raw, _ = collector.read(label, command)
            if label == "fingerprint" and raw and raw.strip():
                result["fingerprint"] = raw.strip()
            elif label == "kernel-runtime" and raw:
                found = re.search(r"(?m)^([0-9a-f]{64})\s+\*?/sys/kernel/notes\s*$", raw)
                result["kernel_runtime_sha256"] = found.group(1) if found else None
            elif label == "battery":
                result["battery"] = parse_battery(raw)
                result["battery_temperature_c"] = result["battery"]["temperature_c"]
        settings_command = "; ".join(
            f"printf '{key}='; settings get {namespace} {key}" for key, namespace in SETTINGS.items())
        raw, _ = collector.read("settings", settings_command)
        fields = dict(line.split("=", 1) for line in (raw or "").splitlines() if "=" in line)
        result["settings"] = {key: fields.get(key) if fields.get(key) not in (None, "", "null") else None
                              for key in SETTINGS}
        # Alias makes the audio setting explicit without silently choosing a
        # device-specific stream index from dumpsys audio.
        result["settings"]["music_volume"] = result["settings"]["volume_music"]
        _, final = collector.read("final-identity", "true", required=True)
        result.update(valid=True, boot_id=collector.boot_id, uptime_s=final["uptime_after_s"])
    except (ValueError, KeyboardInterrupt) as exc:
        result.update(reason="interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc), boot_id=collector.boot_id)
    _write(collector.target / "environment.json", result)
    return result


def capture_memory_baseline(serial, out, count=3, interval_s=10, *, expected_boot_id=None,
                            include_process_meminfo=False, deadline_monotonic_s=None):
    """Acquire lightweight snapshots; detailed process memory is opt-in once."""
    if type(count) is not int or not 1 <= count <= 10:
        raise ValueError("count must be 1..10")
    if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)) or not math.isfinite(interval_s) or not 0 <= interval_s <= 300:
        raise ValueError("interval_s must be finite and 0..300")
    if not isinstance(include_process_meminfo, bool):
        raise ValueError("include_process_meminfo must be boolean")
    collector = _Collector(serial, out, expected_boot_id, deadline_monotonic_s=deadline_monotonic_s)
    result = {"serial": serial, "created_utc": datetime.now(timezone.utc).isoformat(),
              "valid": False, "reason": None, "boot_id": None, "samples": [],
              "aggregate": None, "commands": collector.commands, "read_only": True,
              "process_meminfo": None, "window_restriction": "outside formal power windows",
              "independent": False, "measurement_boundary": "android_kernel_visible_memory"}
    try:
        collector.read("initial-identity", "true", required=True)
        for index in range(count):
            raw, item = collector.read(f"memory-{index + 1}", "cat /proc/meminfo", required=True)
            zram, zitem = collector.read(f"zram-{index + 1}", "cat /sys/block/zram0/mm_stat 2>/dev/null")
            sample = {"index": index + 1, "boot_id": collector.boot_id,
                      "uptime_s": item["uptime_before_s"], "read_end_s": zitem.get("uptime_after_s", item["uptime_after_s"]),
                      "source": item["source"], "zram_source": zitem["source"],
                      "summary": summarize_snapshot(raw, zram if zram and zram.strip() else None)}
            result["samples"].append(sample)
            if index + 1 < count:
                remaining = interval_s
                while remaining > 0:
                    chunk = min(30, remaining)
                    if collector.deadline is not None:
                        budget = collector.deadline - time.monotonic()
                        if budget <= 0:
                            raise ValueError("memory_interval: deadline_exhausted")
                        chunk = min(chunk, budget)
                    time.sleep(chunk)
                    remaining -= chunk
        collector.read("swaps", "cat /proc/swaps")
        if include_process_meminfo:
            _, item = collector.read("process-meminfo", "dumpsys meminfo")
            result["process_meminfo"] = {"available": item["available"], "source": item["source"],
                                         "outside_power_window_required": True}
        collector.read("final-identity", "true", required=True)
        summaries = [sample["summary"] for sample in result["samples"]]
        if len({sample["mem_total_bytes"] for sample in summaries}) != 1:
            raise ValueError("mem_total_changed_in_one_boot")
        available = [sample["mem_available_bytes"] for sample in summaries]
        unavailable = [sample["estimated_unavailable_bytes"] for sample in summaries]
        result["aggregate"] = {
            "snapshot_count": len(summaries), "independent_sample_count": 1,
            "independent": False, "mem_total_bytes": summaries[0]["mem_total_bytes"],
            "mean_mem_available_bytes": fmean(available), "min_mem_available_bytes": min(available),
            "max_mem_available_bytes": max(available), "mean_estimated_unavailable_bytes": fmean(unavailable),
            "snapshot_note": "repeated snapshots of one boot baseline, not independent experiments"}
        result.update(valid=True, boot_id=collector.boot_id)
    except (ValueError, KeyboardInterrupt) as exc:
        result.update(reason="interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc), boot_id=collector.boot_id)
    _write(collector.target / "memory-baseline.json", result)
    return result


def thermal_gate(environment, baseline_temperature_c=None, max_start_c=40.0, max_delta_c=1.5):
    """Reject missing/live-unverified or hot battery evidence before formal runs."""
    for value in (max_start_c, max_delta_c):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("thermal limits must be finite numbers")
    if max_delta_c < 0:
        raise ValueError("max_delta_c cannot be negative")
    temperature = environment.get("battery_temperature_c")
    battery = environment.get("battery") or {}
    result = {"valid": False, "reason": None, "temperature_c": temperature,
              "baseline_temperature_c": baseline_temperature_c,
              "max_start_c": max_start_c, "max_delta_c": max_delta_c,
              "boundary": "battery temperature only; processor thermal zones remain raw"}
    if environment.get("valid") is not True:
        result["reason"] = "environment_capture_invalid"
    elif isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature):
        result["reason"] = "battery_temperature_unavailable"
    elif battery.get("temperature_is_live") is not True:
        result["reason"] = "battery_temperature_not_confirmed_live"
    elif temperature > max_start_c:
        result["reason"] = "battery_temperature_above_start_limit"
    elif baseline_temperature_c is not None:
        if isinstance(baseline_temperature_c, bool) or not isinstance(baseline_temperature_c, (int, float)) or not math.isfinite(baseline_temperature_c):
            raise ValueError("baseline temperature must be finite or null")
        if abs(temperature - baseline_temperature_c) > max_delta_c:
            result["reason"] = "battery_temperature_outside_baseline_tolerance"
        else:
            result["valid"] = True
    else:
        result["valid"] = True
    return result
