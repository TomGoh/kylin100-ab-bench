"""Small experiment control layer over the installed Android Perfetto CLI.

No boot images, power switches, frequency settings or tracing services are
modified. One dedicated finite-duration session is started and stopped by its
recorded PID, with a command-line identity check before signalling it.
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

from .adb_io import pull_root


def write_json(path, value):
    target = Path(path)
    temporary = target.with_name(target.name + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(target)


def adb(serial, *arguments, timeout=12):
    return subprocess.run(["adb", "-s", serial, *arguments], capture_output=True,
                          text=True, timeout=timeout, check=True)


def shell(serial, command, timeout=12):
    return adb(serial, "shell", "su", "0", "sh", "-c", shlex.quote(command), timeout=timeout)


def clock_probe(serial):
    before = time.monotonic()
    proc = shell(serial, "cat /proc/sys/kernel/random/boot_id; cat /proc/uptime; cat /proc/sys/kernel/random/boot_id")
    after = time.monotonic()
    lines = proc.stdout.strip().splitlines()
    if len(lines) != 3 or lines[0] != lines[2]:
        raise ValueError("device restarted or returned an incomplete clock probe")
    try:
        uuid.UUID(lines[0])
        boottime = float(lines[1].split()[0])
    except (ValueError, IndexError) as exc:
        raise ValueError("invalid device identity or uptime") from exc
    if not math.isfinite(boottime) or boottime < 0:
        raise ValueError("invalid device boottime")
    return {"boot_id": lines[0], "t_s": boottime, "raw": proc.stdout,
            "host_before_monotonic_s": before, "host_after_monotonic_s": after,
            "clock": "proc_uptime_boottime", "measurement_validated": False}


def _process_identity(serial, remote, pid):
    proc = shell(serial, f"cat /proc/{pid}/stat; readlink /proc/{pid}/exe; tr '\\000' '\\n' < /proc/{pid}/cmdline", timeout=5)
    lines = proc.stdout.splitlines()
    try:
        stat, executable = lines[0], lines[1]
        starttime = int(stat.rsplit(") ", 1)[1].split()[19])
        observed_pid = int(stat.split(" ", 1)[0])
    except (IndexError, ValueError) as exc:
        raise ValueError("invalid Perfetto process identity") from exc
    if observed_pid != pid or Path(executable).name != "perfetto" or starttime < 0:
        raise ValueError("returned process is not Perfetto")
    if remote + "/config.pbtxt" not in lines[2:] or remote + "/trace.perfetto-trace" not in lines[2:]:
        raise ValueError("Perfetto process does not belong to this capture")
    return {"starttime_ticks": starttime, "executable": executable, "raw": proc.stdout}


def _signal_owned_session(serial, remote, pid, boot_id, process):
    if not re.fullmatch(r"/data/local/tmp/abbench-[a-f0-9]{32}", remote) or type(pid) is not int or pid <= 0:
        raise ValueError("invalid capture process identity")
    uuid.UUID(boot_id)
    starttime, executable = process["starttime_ticks"], process["executable"]
    if type(starttime) is not int or starttime < 0 or Path(executable).name != "perfetto":
        raise ValueError("invalid saved Perfetto identity")
    command = (f"[ \"$(cat /proc/sys/kernel/random/boot_id)\" = {boot_id} ] || exit 8; "
               f"if [ -r /proc/{pid}/cmdline ]; then "
               f"stat=$(cat /proc/{pid}/stat) || exit 7; rest=${{stat##*) }}; set -- $rest; "
               f"[ $# -ge 20 ] || exit 7; shift 19; "
               f"if [ \"$1\" = {starttime} ] && [ \"$(readlink /proc/{pid}/exe)\" = {shlex.quote(executable)} ] "
               f"&& tr '\\000' ' ' < /proc/{pid}/cmdline | grep -F {remote}/ >/dev/null; then "
               f"kill -TERM {pid}; echo owned_session_stopped; else echo original_pid_gone_or_reused; fi; "
               f"else echo original_pid_gone; fi")
    return shell(serial, command, timeout=5)


def start_capture(serial, directory, duration_s=900, mode="unknown", config_path=None):
    if isinstance(duration_s, bool) or not isinstance(duration_s, int) or not 1 <= duration_s <= 1800:
        raise ValueError("capture duration must be 1..1800 whole seconds")
    if mode not in ("native", "xhyper", "unknown"):
        raise ValueError("invalid mode label")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    session = uuid.uuid4().hex
    remote = "/data/local/tmp/abbench-" + session
    result = {"serial": serial, "mode_label": mode, "mode_verified": False,
              "session_id": session, "remote_dir": remote, "duration_limit_s": duration_s,
              "created_utc": datetime.now(timezone.utc).isoformat(), "state": "preparing",
              "measurement_validated": False, "power_boundary": "battery_net"}
    write_json(target / "capture.json", result)
    try:
        result["before"] = clock_probe(serial)
        source = Path(config_path) if config_path else Path(__file__).resolve().parents[1] / "configs/perfetto-power-memory.pbtxt"
        config, count = re.subn(r"(?m)^\s*duration_ms:\s*\d+\s*$", "duration_ms: " + str(duration_s * 1000), source.read_text())
        if count != 1:
            raise ValueError("configuration must have one explicit finite duration")
        config += '\nunique_session_name: "abbench-' + session + '"\n'
        local_config = target / "config.pbtxt"
        local_config.write_text(config)
        shell(serial, "mkdir " + remote)
        adb(serial, "push", str(local_config), remote + "/config.pbtxt")
        proc = shell(serial, "perfetto --background-wait --txt -c " + remote + "/config.pbtxt -o " + remote + "/trace.perfetto-trace", timeout=35)
        (target / "start.stdout.txt").write_text(proc.stdout)
        (target / "start.stderr.txt").write_text(proc.stderr)
        pids = re.findall(r"(?m)^\s*([1-9][0-9]*)\s*$", proc.stdout)
        if len(pids) != 1:
            raise ValueError("Perfetto did not return one session process ID")
        result["pid"] = int(pids[0])
        result["process"] = _process_identity(serial, remote, result["pid"])
        result["armed"] = clock_probe(serial)
        if result["armed"]["boot_id"] != result["before"]["boot_id"]:
            raise ValueError("device restarted while starting telemetry")
        result["state"] = "active"
        write_json(target / "capture.json", result)
        return result
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result.update(state="failed", error=str(exc))
        result["failure_returncode"] = getattr(exc, "returncode", None)
        result["failure_timed_out"] = isinstance(exc, subprocess.TimeoutExpired)
        for field in ("stdout", "stderr"):
            value = getattr(exc, field, None) or ""
            if isinstance(value, bytes):
                value = value.decode(errors="replace")
            (target / ("failure." + field + ".txt")).write_text(value)
        if "pid" in result and "before" in result and "process" in result:
            try:
                cleanup = _signal_owned_session(serial, remote, result["pid"], result["before"]["boot_id"], result["process"])
                result["cleanup"] = {"requested": True, "stdout": cleanup.stdout, "stderr": cleanup.stderr}
            except (ValueError, OSError, subprocess.SubprocessError) as cleanup_error:
                result["cleanup"] = {"requested": False, "error": str(cleanup_error)}
        else:
            result["cleanup"] = {"requested": False, "reason": "session_pid_not_known",
                                 "finite_duration_limit_s": duration_s}
        write_json(target / "capture.json", result)
        raise ValueError("capture start failed: " + str(exc)) from exc


def mark_event(directory, event):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", event):
        raise ValueError("invalid event name")
    target = Path(directory)
    capture = json.loads((target / "capture.json").read_text())
    if capture["state"] != "active":
        raise ValueError("events require an active capture")
    probe = clock_probe(capture["serial"])
    if probe["boot_id"] != capture["before"]["boot_id"]:
        raise ValueError("event belongs to another boot")
    result = {"event": event, **probe}
    # One suite owns each run. O_APPEND preserves existing observations.
    with (target / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def stop_capture(directory):
    target = Path(directory)
    result = json.loads((target / "capture.json").read_text())
    if result["state"] != "active":
        raise ValueError("capture is not active")
    remote, pid = result["remote_dir"], result["pid"]
    if not re.fullmatch(r"/data/local/tmp/abbench-[a-f0-9]{32}", remote) or type(pid) is not int or pid <= 0:
        raise ValueError("invalid capture process identity")
    serial = result["serial"]
    try:
        result["stop_requested"] = clock_probe(serial)
        if result["stop_requested"]["boot_id"] != result["before"]["boot_id"]:
            raise ValueError("device restarted during capture")
        # A PID can be reused after the finite duration expires. Only signal the
        # process whose command line still names this private session directory.
        proc = _signal_owned_session(serial, remote, pid, result["before"]["boot_id"], result["process"])
        (target / "stop.stdout.txt").write_text(proc.stdout)
        (target / "stop.stderr.txt").write_text(proc.stderr)
        deadline = time.monotonic() + 15
        while True:
            proc = shell(serial, f"if [ -r /proc/{pid}/cmdline ] && tr '\\000' ' ' < /proc/{pid}/cmdline | grep -F {remote}/ >/dev/null; then echo running; else echo stopped; fi", timeout=3)
            if proc.stdout.strip() == "stopped":
                break
            if time.monotonic() >= deadline:
                raise ValueError("Perfetto process did not finish before export")
            time.sleep(0.2)
        result["root_export"] = pull_root(serial, remote, target / "root-output")
        trace = target / "root-output/trace.perfetto-trace"
        if not trace.is_file() or trace.stat().st_size == 0:
            raise ValueError("capture has no trace; never substitute zero telemetry")
        result["after"] = clock_probe(serial)
        if result["after"]["boot_id"] != result["before"]["boot_id"]:
            raise ValueError("device restarted during trace export")
        result.update(state="exported", trace=str(trace.resolve()))
        write_json(target / "capture.json", result)
        return result
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result.update(state="failed", error=str(exc))
        write_json(target / "capture.json", result)
        raise ValueError("capture stop failed: " + str(exc)) from exc
