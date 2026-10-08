"""Bounded read-only Android capability snapshots; no capability is auto-approved."""
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

COMMANDS = {
    "identity": "id; getprop ro.build.fingerprint; getprop ro.boot.slot_suffix; getprop sys.boot_completed; cat /proc/sys/kernel/random/boot_id; cat /proc/uptime; cat /proc/cmdline; sha256sum /sys/kernel/notes",
    "battery": "dumpsys battery",
    "battery-help": "dumpsys battery -h",
    "power-supply": 'for p in /sys/class/power_supply/*; do printf "\\n%s\\n" "$p"; cat "$p/type" "$p/uevent" 2>/dev/null; ls "$p"; done',
    "thermal-frequency": 'for p in /sys/class/thermal/thermal_zone*; do printf "\\n%s\\n" "$p"; cat "$p/type" "$p/temp" 2>/dev/null; done; for p in /sys/devices/system/cpu/cpufreq/policy*; do printf "\\n%s\\n" "$p"; cat "$p/scaling_governor" "$p/scaling_min_freq" "$p/scaling_max_freq" 2>/dev/null; done',
    "settings": "settings get system screen_brightness; settings get system screen_brightness_mode; settings get system screen_off_timeout; settings get global stay_on_while_plugged_in; settings get system peak_refresh_rate; settings get system min_refresh_rate",
    "geekbench": "dumpsys package com.primatelabs.geekbench6; pm list features",
    "services": "service list",
    "suspend-counters": "cat /sys/power/suspend_stats/success /sys/power/suspend_stats/fail /sys/kernel/debug/suspend_stats /sys/kernel/debug/suspend_time 2>/dev/null",
    "memory": 'cat /proc/meminfo; cat /proc/swaps; for p in /sys/block/zram*; do printf "\\n%s\\n" "$p"; cat "$p/mm_stat" "$p/disksize" 2>/dev/null; done',
}


def snapshot(serial, directory, timeout_s=12):
    """Save raw outputs and return acquisition status, never measurement validity."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    result = {"serial": serial, "created_utc": datetime.now(timezone.utc).isoformat(),
              "read_only": True, "capabilities_verified": False, "commands": {}}
    for name, command in COMMANDS.items():
        args = ["adb", "-s", serial, "shell", "su", "0", "sh", "-c", shlex.quote(command)]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
            stdout, stderr = proc.stdout, proc.stderr
            item = {"returncode": proc.returncode, "timed_out": False}
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            item = {"returncode": None, "timed_out": True}
        except OSError as exc:
            stdout, stderr = "", str(exc)
            item = {"returncode": None, "timed_out": False, "error": "transport_unavailable"}
        (target / f"{name}.txt").write_text(stdout, encoding="utf-8")
        (target / f"{name}.stderr.txt").write_text(stderr, encoding="utf-8")
        item["command"] = command
        result["commands"][name] = item
    required = ("identity", "battery")
    result["acquisition_complete"] = all(
        result["commands"][key]["returncode"] == 0 and not result["commands"][key]["timed_out"]
        for key in required)
    result["unavailable_or_failed_commands"] = [
        key for key, value in result["commands"].items()
        if value["returncode"] != 0 or value["timed_out"]]
    (target / "snapshot.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
