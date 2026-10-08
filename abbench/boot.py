"""Observe a new Android boot without confusing transport recovery with readiness.

Host times are monotonic probe intervals. Device uptime and bootstat/logcat stay
in their original clock domains; this observer does not infer cold-boot time or
desktop readiness. Reboot is opt-in, and must be announced by the caller.
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


PROBE_SCRIPT = """printf 'boot_id_before='; cat /proc/sys/kernel/random/boot_id
printf 'boot_completed='; getprop sys.boot_completed
printf 'uptime='; cat /proc/uptime
printf 'boot_id_after='; cat /proc/sys/kernel/random/boot_id"""

AUXILIARY_COMMANDS = {
    "bootstat": "bootstat -p",
    "properties": "getprop",
    "boot-events": "logcat -b events -d -v monotonic",
}

DESKTOP_SCRIPT = """printf 'AB_DESKTOP_BOOT_BEFORE\\n'; cat /proc/sys/kernel/random/boot_id
printf 'AB_DESKTOP_HOME\\n'; cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME
printf '\\nAB_DESKTOP_UNLOCKED\\n'; cmd user is-user-unlocked 0
printf '\\nAB_DESKTOP_BOOTANIM\\n'; getprop init.svc.bootanim
printf 'AB_DESKTOP_BOOTANIM_EXIT\\n'; getprop service.bootanim.exit
printf 'AB_DESKTOP_ACTIVITY\\n'; dumpsys activity activities
printf '\\nAB_DESKTOP_BOOT_AFTER\\n'; cat /proc/sys/kernel/random/boot_id"""


def parse_desktop_evidence(raw):
    """Match current resumed fields, not history/intent text mentioning HOME."""
    tags = ("BOOT_BEFORE", "HOME", "UNLOCKED", "BOOTANIM", "BOOTANIM_EXIT", "ACTIVITY", "BOOT_AFTER")
    fields = {}
    for index, tag in enumerate(tags):
        stop = r"^AB_DESKTOP_" + tags[index + 1] + r"\n" if index + 1 < len(tags) else r"\Z"
        match = re.search(r"^AB_DESKTOP_" + tag + r"\n(.*?)" + stop, raw.replace("\r\n", "\n"), re.M | re.S)
        if not match:
            raise ValueError("desktop_evidence_fields_missing")
        fields[tag] = match.group(1).strip()
    before, after = str(uuid.UUID(fields["BOOT_BEFORE"])), str(uuid.UUID(fields["BOOT_AFTER"]))
    if before != after:
        raise ValueError("desktop_probe_crossed_boot")
    components = re.findall(r"(?m)^([A-Za-z][A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)$", fields["HOME"])
    if len(components) != 1 or fields["UNLOCKED"] not in ("true", "false"):
        raise ValueError("home_or_unlock_evidence_unavailable")
    home_package = components[0][0]
    resumed = set(re.findall(r"(?m)^\s*mResumedActivity[=:]\s*ActivityRecord\{[^}\n]*\s([A-Za-z][A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+(?:\s|\})", fields["ACTIVITY"]))
    unlocked = fields["UNLOCKED"] == "true"
    animation_stopped = (fields["BOOTANIM"] == "stopped" or fields["BOOTANIM_EXIT"] == "1") and fields["BOOTANIM"] not in ("running", "restarting")
    return {"boot_id": before, "home_package": home_package, "user_unlocked": unlocked,
            "boot_animation_stopped": animation_stopped, "resumed_packages": sorted(resumed),
            "launcher_resumed": resumed == {home_package},
            "satisfied": unlocked and animation_stopped and resumed == {home_package},
            "first_draw_verified": False}


def parse_probe(stdout):
    """Require matching boot identities around the property and uptime reads."""
    fields = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key not in ("boot_id_before", "boot_completed", "uptime", "boot_id_after"):
            continue
        if key in fields:
            raise ValueError("duplicate_probe_field")
        fields[key] = value.strip()
    if set(fields) != {"boot_id_before", "boot_completed", "uptime", "boot_id_after"}:
        raise ValueError("missing_probe_field")
    try:
        before = str(uuid.UUID(fields["boot_id_before"]))
        after = str(uuid.UUID(fields["boot_id_after"]))
        uptime_s = float(fields["uptime"].split()[0])
    except (ValueError, IndexError) as exc:
        raise ValueError("invalid_probe_value") from exc
    if before != after:
        raise ValueError("intra_probe_boot_id_changed")
    if not math.isfinite(uptime_s) or uptime_s < 0:
        raise ValueError("invalid_probe_uptime")
    return {"boot_id": before, "boot_completed_raw": fields["boot_completed"],
            "system_complete": fields["boot_completed"] == "1",
            "device_uptime_s": uptime_s,
            "device_clock": "/proc/uptime (not converted to host monotonic)"}


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


class _Acquisition:
    def __init__(self, serial, target, deadline, command_timeout_s):
        self.serial = serial
        self.target = target
        self.deadline = deadline
        self.command_timeout_s = command_timeout_s
        self.events = []

    def run(self, label, args, deadline=None):
        start = time.monotonic()
        limit = self.deadline if deadline is None else min(deadline, self.deadline)
        remaining = limit - start
        if remaining <= 0:
            return None
        timeout = min(self.command_timeout_s, remaining)
        stdout, stderr = "", ""
        item = {"kind": label, "host_start_s": start, "host_end_s": None,
                "timeout_s": timeout, "returncode": None, "timed_out": False,
                "error": None, "argv": args}
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            stdout, stderr = _text(proc.stdout), _text(proc.stderr)
            item["returncode"] = proc.returncode
            if proc.returncode != 0:
                item["error"] = "command_failed"
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _text(exc.stdout), _text(exc.stderr)
            item["timed_out"] = True
            item["error"] = "command_timeout"
        except OSError as exc:
            stderr = str(exc)
            item["error"] = "transport_unavailable"
        item["host_end_s"] = time.monotonic()
        # The timeout is bounded by the remaining deadline. Process scheduling
        # may return slightly late; late output cannot establish readiness.
        item["within_deadline"] = item["host_end_s"] <= self.deadline
        stem = f"{len(self.events):04d}-{label}"
        item["stdout_file"] = f"{stem}.stdout.txt"
        item["stderr_file"] = f"{stem}.stderr.txt"
        (self.target / item["stdout_file"]).write_text(stdout, encoding="utf-8")
        (self.target / item["stderr_file"]).write_text(stderr, encoding="utf-8")
        item["stdout"] = stdout
        self.events.append(item)
        self.journal(item)
        return item

    def journal(self, item):
        with (self.target / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")

    def shell(self, label, command, deadline=None):
        args = ["adb", "-s", self.serial, "shell", "su", "0", "sh", "-c",
                shlex.quote(command)]
        return self.run(label, args, deadline)

    def probe(self, label):
        item = self.shell(label, PROBE_SCRIPT)
        if item is None:
            return None
        if item["error"] is None and item["within_deadline"]:
            try:
                item["probe"] = parse_probe(item["stdout"])
            except ValueError as exc:
                item["error"] = str(exc)
        elif not item["within_deadline"]:
            item["error"] = "deadline_exceeded"
        # Preserve the parsed or failed interpretation alongside the raw call.
        self.journal({"kind": "probe-interpretation", "source": item["stdout_file"],
                      "probe": item.get("probe"), "error": item["error"]})
        return item


def measure_boot(serial, directory, timeout_s=180, reboot=False, *,
                 poll_s=1, command_timeout_s=3, desktop=False):
    """Save and return a bounded observation of an old-to-new boot transition.

    Default operation waits for an externally triggered reboot. It cannot know
    when a physical key or another process triggered that reboot. With explicit
    ``reboot=True`` it sends one normal ``adb reboot`` and reports a warm-reboot
    request-to-system-completion proxy. Optional desktop evidence establishes
    resumed/unlocked launcher state, never first-frame or desktop-drawn time.
    """
    if not isinstance(serial, str) or not serial.strip():
        raise ValueError("serial must be a nonempty string")
    for name, value in (("timeout_s", timeout_s), ("poll_s", poll_s),
                        ("command_timeout_s", command_timeout_s)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if timeout_s > 300:
        raise ValueError("timeout_s must not exceed 300 seconds")
    if command_timeout_s > 5:
        raise ValueError("command_timeout_s must not exceed 5 seconds")
    if not isinstance(reboot, bool):
        raise ValueError("reboot must be a boolean")
    if not isinstance(desktop, bool):
        raise ValueError("desktop must be a boolean")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    deadline = start + timeout_s
    collector = _Acquisition(serial, target, deadline, command_timeout_s)
    result = {
        "serial": serial, "created_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": not reboot, "reboot_requested": reboot,
        "host_clock": "time.monotonic on the host running adb",
        "host_start_s": start, "host_deadline_s": deadline, "host_end_s": None,
        "poll_requested_s": poll_s, "command_timeout_requested_s": command_timeout_s,
        "old_boot_id": None, "new_boot_id": None, "boot_id": None, "valid": False,
        "status": "failed", "reason": None, "elapsed_s": None,
        "reboot_request_host_s": None, "external_trigger_host_s": None,
        "measurement_boundary": (
            "adb_reboot_request_to_new_boot_system_complete_warm_proxy" if reboot else
            "new_boot_access_observation_to_system_complete_proxy_external_trigger_unknown"),
        "new_boot_access_interval_host_s": None,
        "boot_transition_interval_host_s": None,
        "system_complete_interval_host_s": None,
        "request_to_system_complete_interval_s": None,
        "new_boot_access_to_system_complete_interval_s": None,
        "request_to_system_complete_observed_s": None,
        "new_boot_access_to_system_complete_observed_s": None,
        "desktop_ready": {"value": None, "reason": "device_adapter_not_implemented"},
        "desktop_resumed_observed": None,
        "request_to_desktop_resumed_observed_s": None,
        "request_to_desktop_resumed_interval_s": None,
        "auxiliary": {}, "limitations": [
            "not a full power-off cold-boot measurement",
            "ADB availability may follow actual system completion",
            "probe intervals include transport and command latency",
            "bootstat, properties and logcat retain raw units and clock domains",
        ],
    }

    def finish(reason=None):
        result["host_end_s"] = time.monotonic()
        result["reason"] = reason
        result["command_count"] = len(collector.events)
        result["failed_commands"] = [
            {"kind": item["kind"], "error": item["error"],
             "stdout_file": item["stdout_file"], "stderr_file": item["stderr_file"]}
            for item in collector.events if item["error"] is not None]
        temporary = target / "boot.json.tmp"
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                        allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(target / "boot.json")
        return result

    baseline = collector.probe("baseline")
    if not baseline or not baseline.get("probe"):
        return finish("baseline_boot_id_unavailable")
    old_id = baseline["probe"]["boot_id"]
    result["old_boot_id"] = old_id
    last_old = baseline
    request = None
    if reboot:
        request = collector.run("reboot-request", ["adb", "-s", serial, "reboot"])
        if request is None:
            return finish("deadline_before_reboot_request")
        result["reboot_request_host_s"] = request["host_start_s"]
        if request["error"] is not None or not request["within_deadline"]:
            return finish("reboot_request_failed")

    first_access = None
    last_not_ready = None
    ready = None
    while time.monotonic() < deadline:
        item = collector.probe("boot-probe")
        if item is None:
            break
        parsed = item.get("probe")
        if item["error"] == "intra_probe_boot_id_changed":
            return finish("boot_id_changed_during_probe")
        if parsed:
            boot_id = parsed["boot_id"]
            if boot_id == old_id:
                if result["new_boot_id"] is not None:
                    return finish("boot_id_changed_again")
                last_old = item
            else:
                if result["new_boot_id"] is None:
                    result["new_boot_id"] = boot_id
                    first_access = item
                    result["new_boot_access_interval_host_s"] = {
                        "lower": item["host_start_s"], "upper": item["host_end_s"]}
                    result["boot_transition_interval_host_s"] = {
                        "lower": last_old["host_start_s"], "upper": item["host_end_s"],
                        "meaning": "boot identity transition, not external trigger time"}
                elif result["new_boot_id"] != boot_id:
                    return finish("boot_id_changed_again")
                if parsed["system_complete"]:
                    ready = item
                    break
                last_not_ready = item
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_s, remaining))

    if ready is None:
        return finish("new_boot_system_complete_timeout")
    lower = (last_not_ready["host_start_s"] if last_not_ready else
             request["host_start_s"] if request else None)
    lower_reason = ("last_new_boot_not_ready_probe_start" if last_not_ready else
                    "reboot_request_start_no_new_boot_not_ready_observed" if request else
                    "no_new_boot_not_ready_observed")
    upper = ready["host_end_s"]
    result["system_complete_interval_host_s"] = {
        "lower": lower, "upper": upper, "lower_reason": lower_reason,
        "upper_reason": "first_new_boot_complete_probe_end"}
    if request:
        request_start = request["host_start_s"]
        result["request_to_system_complete_interval_s"] = {
            "lower": max(0, lower - request_start), "upper": upper - request_start}
    # If completion happened before ADB became accessible, zero is only a
    # conservative lower bound. It is not a measured zero-duration boot.
    access = result["new_boot_access_interval_host_s"]
    result["new_boot_access_to_system_complete_interval_s"] = {
        "lower": max(0, lower - access["upper"]) if last_not_ready else 0,
        "upper": max(0, upper - access["lower"]),
        "meaning": "observation proxy; actual completion may precede first access"}

    # Reserve one bounded final identity read before optional diagnostics.
    auxiliary_deadline = deadline - command_timeout_s
    if desktop:
        result["desktop_ready"] = {"value": None, "reason": "first_draw_not_verified"}
        result["desktop_resumed_observed"] = {"value": None, "reason": "desktop_observation_timeout",
                                               "first_draw_verified": False}
        desktop_deadline = min(auxiliary_deadline, time.monotonic() + 15)
        last_desktop_not_ready = None
        while time.monotonic() < desktop_deadline:
            item = collector.shell("desktop-state", DESKTOP_SCRIPT, desktop_deadline)
            if item is None:
                break
            if item["error"] is not None:
                result["desktop_resumed_observed"]["reason"] = item["error"]
                break
            try:
                evidence = parse_desktop_evidence(item["stdout"])
            except ValueError as exc:
                if str(exc) == "desktop_probe_crossed_boot":
                    return finish("desktop_observation_cross_boot")
                result["desktop_resumed_observed"]["reason"] = str(exc)
                break
            if evidence["boot_id"] != result["new_boot_id"]:
                return finish("desktop_observation_cross_boot")
            if evidence["satisfied"]:
                desktop_lower = (last_desktop_not_ready["host_start_s"] if last_desktop_not_ready else
                                 request["host_start_s"] if request else None)
                result["desktop_resumed_observed"] = {
                    "value": True, "reason": None, "evidence": evidence,
                    "source": item["stdout_file"], "first_draw_verified": False,
                    "interval_host_s": {"lower": desktop_lower, "upper": item["host_end_s"]},
                    "boundary": "HOME launcher resumed, user unlocked, animation stopped; not first draw"}
                if request:
                    result["request_to_desktop_resumed_interval_s"] = {
                        "lower": max(0, desktop_lower - request["host_start_s"]),
                        "upper": item["host_end_s"] - request["host_start_s"]}
                    result["request_to_desktop_resumed_observed_s"] = (
                        result["request_to_desktop_resumed_interval_s"]["upper"])
                break
            last_desktop_not_ready = item
            time.sleep(min(poll_s, max(0, desktop_deadline - time.monotonic())))
    for label, command in AUXILIARY_COMMANDS.items():
        item = collector.shell(label, command, auxiliary_deadline)
        if item is None:
            result["auxiliary"][label] = {"available": False, "reason": "deadline_budget"}
        else:
            result["auxiliary"][label] = {
                "available": item["error"] is None, "reason": item["error"],
                "stdout_file": item["stdout_file"], "stderr_file": item["stderr_file"],
                "units": "raw, mixed or unverified", "clock": "source-specific, not host monotonic"}
    final = collector.probe("final-identity")
    if not final or not final.get("probe"):
        return finish("final_boot_id_unavailable")
    if final["probe"]["boot_id"] != result["new_boot_id"]:
        return finish("boot_id_changed_during_collection")
    if not final["probe"]["system_complete"]:
        return finish("boot_completed_not_stable")
    result["valid"] = True
    result["status"] = "observed"
    result["boot_id"] = result["new_boot_id"]
    if request:
        result["request_to_system_complete_observed_s"] = (
            result["request_to_system_complete_interval_s"]["upper"])
    result["new_boot_access_to_system_complete_observed_s"] = (
        result["new_boot_access_to_system_complete_interval_s"]["upper"])
    # Observed point values are upper bounds; no exact elapsed time is claimed.
    return finish()
