"""Dismiss only an explicitly nonsecure keyguard with normal Android controls."""
import math
import re
import subprocess
import time
import uuid
from pathlib import Path

from .capture import shell, write_json


def parse_keyguard(raw):
    if raw.count("KeyguardServiceDelegate") != 1 or raw.count("KeyguardStateMonitor") != 1:
        return {"secure": None, "showing": None, "monitor_showing": None}
    delegate, monitor = raw.split("KeyguardServiceDelegate", 1)[1].split("KeyguardStateMonitor", 1)
    def boolean(text, field):
        values = re.findall(r"(?m)^\s*" + field + r"\s*=\s*(true|false)\s*$", text)
        return values[0] == "true" if len(values) == 1 else None
    return {"secure": boolean(delegate, "secure"), "showing": boolean(delegate, "showing"),
            "monitor_showing": boolean(monitor, "mIsShowing")}


def prepare_ui(serial, out, expected_boot_id=None, *, deadline_monotonic_s=None):
    """Wake, normally dismiss a nonsecure keyguard, verify, then go home.

    Credential locks and unknown policy are rejected. No swipe coordinates,
    credentials, lock configuration or permanent screen policies are changed.
    """
    if deadline_monotonic_s is not None and (isinstance(deadline_monotonic_s, bool) or
            not isinstance(deadline_monotonic_s, (int, float)) or not math.isfinite(deadline_monotonic_s)):
        raise ValueError("deadline_monotonic_s must be finite or null")
    target = Path(out)
    target.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = min(started + 30, deadline_monotonic_s) if deadline_monotonic_s is not None else started + 30
    result = {"valid": False, "reason": None, "serial": serial, "boot_id": None,
              "before": None, "after": None, "initial_policy": None, "final_policy": None,
              "dismiss_requested": False, "commands": [], "security_settings_changed": False}

    def command(label, text, limit=None):
        effective_deadline = min(deadline, limit) if limit is not None else deadline
        remaining = effective_deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("deadline_exhausted")
        item = {"label": label, "command": text, "host_before_s": time.monotonic(),
                "error": None, "timeout_s": min(8, remaining)}
        stdout, stderr = "", ""
        try:
            proc = shell(serial, text, timeout=item["timeout_s"])
            stdout, stderr = proc.stdout, proc.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            stdout, stderr = getattr(exc, "stdout", "") or "", getattr(exc, "stderr", "") or str(exc)
            item["error"] = type(exc).__name__
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        item["host_after_s"] = time.monotonic()
        stem = f"{len(result['commands']):03d}-{label}"
        item.update(source=stem + ".stdout.txt", stderr_source=stem + ".stderr.txt")
        (target / item["source"]).write_text(stdout, encoding="utf-8")
        (target / item["stderr_source"]).write_text(stderr, encoding="utf-8")
        result["commands"].append(item)
        if item["host_after_s"] >= effective_deadline:
            raise ValueError("deadline_exhausted")
        if item["error"]:
            raise ValueError(label + ": " + item["error"])
        return stdout.replace("\r\n", "\n")

    def clock(label):
        raw = command(label, "cat /proc/sys/kernel/random/boot_id; cat /proc/uptime; cat /proc/sys/kernel/random/boot_id")
        lines = raw.strip().splitlines()
        if len(lines) != 3 or lines[0] != lines[2]:
            raise ValueError("clock_identity_unavailable_or_cross_boot")
        boot = str(uuid.UUID(lines[0]))
        uptime = float(lines[1].split()[0])
        if not math.isfinite(uptime) or uptime < 0:
            raise ValueError("clock_uptime_invalid")
        return {"boot_id": boot, "t_s": uptime, "clock": "proc_uptime_boottime"}

    try:
        result["before"] = clock("initial-clock")
        result["boot_id"] = result["before"]["boot_id"]
        if expected_boot_id is not None and expected_boot_id != result["boot_id"]:
            raise ValueError("unexpected_boot_id")
        policy = parse_keyguard(command("initial-policy", "dumpsys window policy"))
        result["initial_policy"] = policy
        if policy["secure"] is None or policy["showing"] is None or policy["monitor_showing"] is None:
            raise ValueError("keyguard_policy_unavailable")
        if policy["secure"] and (policy["showing"] or policy["monitor_showing"]):
            raise ValueError("credential_locked")
        command("wake", "input keyevent 224")
        if policy["showing"] or policy["monitor_showing"]:
            result["dismiss_requested"] = True
            command("dismiss", "wm dismiss-keyguard")
        wait_deadline = min(deadline, time.monotonic() + 10)
        while True:
            if time.monotonic() >= wait_deadline:
                raise ValueError("keyguard_dismiss_observation_timeout")
            policy = parse_keyguard(command("verify-policy", "dumpsys window policy", wait_deadline))
            result["final_policy"] = policy
            if policy["secure"] is None:
                raise ValueError("keyguard_policy_unavailable")
            if policy["showing"] is False and policy["monitor_showing"] is False:
                break
            if policy["secure"] is not False:
                raise ValueError("credential_or_unknown_keyguard_after_wake")
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("keyguard_dismiss_observation_timeout")
            time.sleep(min(0.5, remaining))
        result["after"] = clock("final-clock")
        if result["after"]["boot_id"] != result["boot_id"]:
            raise ValueError("device_restarted_during_ui_prepare")
        command("home", "input keyevent 3")
        result["valid"] = True
    except (ValueError, IndexError, KeyboardInterrupt) as exc:
        result["reason"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
    write_json(target / "ui-control.json", result)
    return result
