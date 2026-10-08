"""Automated screen-on and screen-off windows with bounded, reversible controls.

Only screen timeout and stay-awake policy are changed. Screen-off windows have
no periodic device queries or Perfetto session. Internal power remains unknown
until the caller supplies explicitly validated units, counter and supply scope.
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


def parse_screen_state(raw, display_raw=None):
    """Require concordant global wakefulness and display power evidence."""
    wake = set(re.findall(r"(?m)^\s*m(?:Global)?Wakefulness=(Awake|Asleep|Dozing|Dreaming)\s*$", raw))
    display = set(re.findall(r"Display Power:\s*state=(ON|OFF|DOZE|DOZE_SUSPEND)\b", raw))
    if not display and display_raw is not None:
        default_lines = [line for line in display_raw.splitlines()
                         if "DisplayDeviceInfo{" in line and
                         ("FLAG_DEFAULT_DISPLAY" in line or "FLAG_ALLOWED_TO_BE_DEFAULT_DISPLAY" in line)]
        if len(default_lines) == 1:
            display = set(re.findall(r"\b(?:state|committedState) (ON|OFF|DOZE|DOZE_SUSPEND)\b", default_lines[0]))
    if wake == {"Awake"} and display == {"ON"}:
        return "on"
    if wake == {"Asleep"} and display == {"OFF"}:
        return "off"
    return None


def parse_suspend_stats(raw):
    count = re.findall(r"(?m)^success:\s*(\d+)\s*$", raw)
    elapsed = re.findall(r"(?m)^total suspend time:\s*(\d+)\s*ms\s*$", raw)
    return {"success": int(count[0]) if len(count) == 1 else None,
            "total_suspend_ms": int(elapsed[0]) if len(elapsed) == 1 else None}


def _start_capture(*args, **kwargs):
    from .capture import start_capture
    return start_capture(*args, **kwargs)


def _stop_capture(*args, **kwargs):
    from .capture import stop_capture
    return stop_capture(*args, **kwargs)


def _mark_event(*args, **kwargs):
    from .capture import mark_event
    return mark_event(*args, **kwargs)


def _decode(raw):
    return raw.decode(errors="replace") if isinstance(raw, bytes) else raw or ""


class _Device:
    def __init__(self, serial, target, timeout_s):
        self.serial, self.target, self.timeout_s = serial, target, timeout_s
        self.index = 0

    def event(self, event):
        with (self.target / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")

    def shell(self, label, command):
        item = {"event": label, "host_before_s": time.monotonic(), "command": command,
                "returncode": None, "timed_out": False}
        args = ["adb", "-s", self.serial, "shell", "su", "0", "sh", "-c", shlex.quote(command)]
        stdout, stderr = "", ""
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout_s)
            stdout, stderr = _decode(proc.stdout), _decode(proc.stderr)
            item["returncode"] = proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _decode(exc.stdout), _decode(exc.stderr)
            item["timed_out"] = True
        except OSError as exc:
            stderr = str(exc)
        item["host_after_s"] = time.monotonic()
        stem = f"{self.index:03d}-{label}"
        self.index += 1
        item.update(stdout_file=stem + ".stdout.txt", stderr_file=stem + ".stderr.txt")
        (self.target / item["stdout_file"]).write_text(stdout, encoding="utf-8")
        (self.target / item["stderr_file"]).write_text(stderr, encoding="utf-8")
        self.event(item)
        if item["returncode"] != 0 or item["timed_out"]:
            raise ValueError(label + ": device_command_failed")
        return stdout, item

    def boot_id(self):
        raw, _ = self.shell("boot-identity", "cat /proc/sys/kernel/random/boot_id")
        return str(uuid.UUID(raw.strip()))

    def screen(self, expected=None, wait_s=5):
        deadline = time.monotonic() + wait_s
        while True:
            raw, item = self.shell("screen-state", "dumpsys power")
            display_raw, display_item = self.shell("display-state", "dumpsys display")
            state = parse_screen_state(raw, display_raw)
            if expected is None or state == expected:
                return {"state": state, "source": item["stdout_file"],
                        "display_source": display_item["stdout_file"],
                        "host_before_s": item["host_before_s"], "host_after_s": item["host_after_s"]}
            if time.monotonic() >= deadline:
                raise ValueError("screen_state_not_verified_" + expected)
            time.sleep(min(0.3, max(0, deadline - time.monotonic())))

    def endpoint(self, label, battery_path, usb_path):
        command = "\n".join([
            "read_value() { if [ -r \"$1\" ]; then cat \"$1\" 2>/dev/null || printf 'NA\\n'; else printf 'NA\\n'; fi; }",
            "printf 'boot_before='; cat /proc/sys/kernel/random/boot_id",
            "read t rest < /proc/uptime; printf 'read_begin_s=%s\\n' \"$t\"",
            "printf 'voltage_candidate='; read_value " + shlex.quote(battery_path + "/voltage_now"),
            "printf 'charge_candidate='; read_value " + shlex.quote(battery_path + "/charge_counter"),
            "printf 'physical_usb_online='; read_value " + shlex.quote(usb_path),
            "read t rest < /proc/uptime; printf 'read_end_s=%s\\n' \"$t\"",
            "printf 'boot_after='; cat /proc/sys/kernel/random/boot_id",
        ])
        raw, item = self.shell(label, command)
        fields = {}
        for line in raw.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in fields:
                    raise ValueError("duplicate_endpoint_field")
                fields[key] = value.strip()
        try:
            boot = str(uuid.UUID(fields["boot_before"]))
            after = str(uuid.UUID(fields["boot_after"]))
            begin, end = float(fields["read_begin_s"]), float(fields["read_end_s"])
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid_endpoint_identity_or_clock") from exc
        if boot != after:
            raise ValueError("endpoint_crossed_boot")
        if not all(math.isfinite(value) for value in (begin, end)) or begin < 0 or end < begin:
            raise ValueError("invalid_endpoint_clock")
        def candidate(key):
            text = fields.get(key, "")
            return int(text) if re.fullmatch(r"-?\d+", text) else None
        usb = candidate("physical_usb_online")
        return {"boot_id": boot, "read_begin_s": begin, "read_end_s": end,
                "voltage_uv_candidate": candidate("voltage_candidate"),
                "charge_uah_candidate": candidate("charge_candidate"),
                "physical_usb_online": bool(usb) if usb in (0, 1) else None,
                "clock": "proc_uptime_CLOCK_BOOTTIME_candidate",
                "units_validated": False, "counter_validated": False,
                "host_before_s": item["host_before_s"], "host_after_s": item["host_after_s"],
                "source": item["stdout_file"]}

    def suspend(self, label):
        try:
            raw, item = self.shell(label, "dumpsys suspend_control_internal")
            return {**parse_suspend_stats(raw), "available": True, "source": item["stdout_file"]}
        except ValueError as exc:
            return {"success": None, "total_suspend_ms": None,
                    "available": False, "reason": str(exc)}


def run_idle(serial, directory, kind="screen_on_idle", duration_s=None, mode="unknown", *,
             power_config=None, battery_path="/sys/class/power_supply/battery",
             usb_online_path="/sys/class/power_supply/usb/online", command_timeout_s=8):
    """Run one automatic window and restore changed settings in its original boot.

    ``duration_s`` is required; the suite chooses formal or short validation
    durations. Success denotes a collected window, not valid power metrology.
    """
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError("serial must be a nonempty string")
    if kind not in ("screen_on_idle", "screen_off_standby"):
        raise ValueError("unsupported idle kind")
    if type(duration_s) is not int or not 1 <= duration_s <= 1800:
        raise ValueError("duration_s must be 1..1800 whole seconds")
    if mode not in ("native", "xhyper", "unknown"):
        raise ValueError("invalid mode label")
    if isinstance(command_timeout_s, bool) or not isinstance(command_timeout_s, (int, float)) or not math.isfinite(command_timeout_s) or not 0 < command_timeout_s <= 15:
        raise ValueError("command_timeout_s must be >0 and <=15")
    for path in (battery_path, usb_online_path):
        if not isinstance(path, str) or not path.startswith("/sys/") or "\n" in path or "\r" in path or ".." in path.split("/"):
            raise ValueError("telemetry paths must be explicit sysfs paths")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    device = _Device(serial, target, command_timeout_s)
    result = {"serial": serial, "kind_requested": kind, "kind_observed": None,
              "mode_label": mode, "mode_verified": False, "duration_requested_s": duration_s,
              "created_utc": datetime.now(timezone.utc).isoformat(), "valid": False,
              "reason": None, "boot_id": None, "elapsed_boottime_s": None,
              "elapsed_boottime_interval_s": None, "start": None, "end": None,
              "screen_before": None, "screen_start": None, "screen_end": None,
              "suspend_before": None, "suspend_after": None, "suspend_delta": None,
              "standby_label_reason": None,
              "settings": {}, "restore": {}, "capture": None,
              "capture_window_start": None, "capture_window_end": None,
              "power": {"valid": False, "reason": "measurement_not_validated",
                        "mean_w": None, "energy_j": None},
              "wake_overhead": None,
              "limitations": ["screen state verified at boundaries, not continuously",
                               "raw USB online is physical status, not verified input isolation",
                               "endpoint readings and end wakeup contribute overhead"]}
    changed = []
    original_screen = None
    capture_active = False
    try:
        result["boot_id"] = device.boot_id()
        result["screen_before"] = device.screen()
        original_screen = result["screen_before"]["state"]
        if original_screen is None:
            raise ValueError("initial_screen_state_unavailable")
        desired = [("system", "screen_off_timeout", str(duration_s * 1000 + 120000)),
                   ("global", "stay_on_while_plugged_in", "0")]
        for namespace, key, value in desired:
            raw, _ = device.shell("settings-read", f"settings get {namespace} {key}")
            old = raw.strip()
            if old != "null" and not re.fullmatch(r"\d+", old):
                raise ValueError("original_screen_setting_unavailable_" + key)
            result["settings"][key] = {"namespace": namespace, "original": old, "requested": value}
            if old != value:
                # Mark before writing: a timed-out write may still have landed.
                changed.append((namespace, key, old))
                device.shell("settings-write", f"settings put {namespace} {key} {value}")
        device.shell("screen-wake", "input keyevent 224")
        device.shell("home", "input keyevent 3")
        device.screen("on")
        if kind == "screen_on_idle":
            result["capture"] = _start_capture(serial, target / "capture",
                                               duration_s=min(1800, duration_s + 60), mode=mode)
            capture_active = True
        else:
            device.shell("screen-sleep", "input keyevent 223")
        result["screen_start"] = device.screen("on" if kind == "screen_on_idle" else "off")
        result["suspend_before"] = device.suspend("suspend-before")
        result["start"] = device.endpoint("endpoint-start", battery_path, usb_online_path)
        if result["start"]["boot_id"] != result["boot_id"]:
            raise ValueError("device_restarted_before_window")
        if capture_active:
            result["capture_window_start"] = _mark_event(target / "capture", "idle_window_start")
        window_start = time.monotonic()
        window_end = window_start + duration_s
        device.event({"event": "window-start", "host_s": window_start,
                      "no_device_polling": kind == "screen_off_standby"})
        while time.monotonic() < window_end:
            # Host-only progress keeps the device free of periodic wakeups.
            time.sleep(min(30, max(0, window_end - time.monotonic())))
            device.event({"event": "host-wait-progress", "host_s": time.monotonic()})
        device.event({"event": "window-end", "host_s": time.monotonic()})
        if capture_active:
            result["capture_window_end"] = _mark_event(target / "capture", "idle_window_end")
        result["screen_end"] = device.screen("on" if kind == "screen_on_idle" else "off")
        if kind == "screen_off_standby":
            wake_begin = time.monotonic()
            device.shell("end-screen-wake", "input keyevent 224")
            result["wake_overhead"] = {"host_wake_request_s": wake_begin,
                                       "host_wake_return_s": time.monotonic(),
                                       "host_first_end_query_s": result["screen_end"]["host_before_s"],
                                       "included_in_endpoint_window": True}
        result["end"] = device.endpoint("endpoint-end", battery_path, usb_online_path)
        if result["wake_overhead"] is not None:
            wake = result["wake_overhead"]
            wake["host_first_end_query_to_endpoint_return_s"] = (
                result["end"]["host_after_s"] - wake["host_first_end_query_s"])
            wake["host_wake_request_to_endpoint_interval_s"] = {
                "lower": result["end"]["host_before_s"] - wake["host_wake_request_s"],
                "upper": result["end"]["host_after_s"] - wake["host_wake_request_s"]}
        if result["end"]["boot_id"] != result["boot_id"]:
            raise ValueError("device_restarted_during_window")
        start, end = result["start"], result["end"]
        elapsed = end["read_begin_s"] - start["read_begin_s"]
        if elapsed <= 0:
            raise ValueError("non_increasing_endpoint_time")
        result["elapsed_boottime_s"] = elapsed
        result["elapsed_boottime_interval_s"] = {
            "lower": max(0, end["read_begin_s"] - start["read_end_s"]),
            "upper": end["read_end_s"] - start["read_begin_s"]}
        result["suspend_after"] = device.suspend("suspend-after")
        before, after = result["suspend_before"], result["suspend_after"]
        if all(value is not None for value in (before["success"], after["success"],
                                               before["total_suspend_ms"], after["total_suspend_ms"])):
            count = after["success"] - before["success"]
            elapsed_ms = after["total_suspend_ms"] - before["total_suspend_ms"]
            result["suspend_delta"] = {"success": count, "total_suspend_ms": elapsed_ms,
                                       "valid": count >= 0 and elapsed_ms >= 0}
        delta = result["suspend_delta"]
        deep = delta and delta["valid"] and delta["success"] > 0 and delta["total_suspend_ms"] > 0
        result["kind_observed"] = ("screen_on_idle" if kind == "screen_on_idle" else
                                   "screen_off_standby" if deep else "screen_off_idle")
        if kind == "screen_off_standby" and not deep:
            result["standby_label_reason"] = ("suspend_stats_unavailable" if delta is None else
                                               "suspend_stats_decreased" if not delta["valid"] else
                                               "no_verified_successful_suspend")
        if power_config is not None:
            if not isinstance(power_config, dict) or power_config.get("units_validated") is not True or power_config.get("counter_validated") is not True:
                result["power"]["reason"] = "units_or_counter_not_validated"
            else:
                from .power import endpoint
                def validated(sample):
                    return {"boot_id": sample["boot_id"], "t_s": sample["read_begin_s"],
                            "voltage_uv": sample["voltage_uv_candidate"],
                            "charge_uah": sample["charge_uah_candidate"],
                            "external_online": power_config.get("external_online_verified")}
                result["power"] = endpoint(validated(start), validated(end), power_config)
        result["valid"] = True
    except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        result["reason"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
    finally:
        if capture_active:
            try:
                result["capture"] = _stop_capture(target / "capture")
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                result["valid"] = False
                result["capture_stop_error"] = str(exc)
                result["reason"] = result["reason"] or "capture_stop_failed: " + str(exc)
        if changed or original_screen is not None:
            try:
                same_boot = device.boot_id() == result["boot_id"]
            except (ValueError, OSError, subprocess.SubprocessError):
                same_boot = False
            for namespace, key, old in reversed(changed):
                if not same_boot:
                    result["restore"][key] = {"restored": False, "reason": "original_boot_not_verified"}
                    continue
                try:
                    command = (f"settings delete {namespace} {key}" if old == "null" else
                               f"settings put {namespace} {key} {old}")
                    command = ("[ \"$(cat /proc/sys/kernel/random/boot_id)\" = " +
                               shlex.quote(result["boot_id"]) + " ] || exit 41; " + command)
                    device.shell("settings-restore", command)
                    check, _ = device.shell("settings-restore-check", f"settings get {namespace} {key}")
                    restored = check.strip() == old
                    result["restore"][key] = {"restored": restored,
                                                "reason": None if restored else "restore_readback_mismatch"}
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    result["restore"][key] = {"restored": False, "reason": str(exc)}
            if same_boot and original_screen is not None:
                try:
                    guard = ("[ \"$(cat /proc/sys/kernel/random/boot_id)\" = " +
                             shlex.quote(result["boot_id"]) + " ] || exit 41; ")
                    device.shell("screen-restore", guard + "input keyevent " + ("224" if original_screen == "on" else "223"))
                    restored_screen = device.screen(original_screen)
                    result["restore"]["screen"] = {"restored": restored_screen["state"] == original_screen,
                                                      "reason": None}
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    result["restore"]["screen"] = {"restored": False, "reason": str(exc)}
            elif original_screen is not None:
                result["restore"]["screen"] = {"restored": False, "reason": "original_boot_not_verified"}
        result["settings_restored"] = all(item["restored"] for item in result["restore"].values())
        if not result["settings_restored"]:
            result["valid"] = False
            result["reason"] = result["reason"] or "settings_restore_incomplete"
        temporary = target / "idle.json.partial"
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(target / "idle.json")
    return result
