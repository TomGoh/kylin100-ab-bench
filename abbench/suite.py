"""串行编排现有设备采集器；镜像切换仅调用执行方交付的外部适配器。"""
import importlib
import json
import math
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .device_lock import serialized


def _call(module, function, *args, **kwargs):
    return getattr(importlib.import_module("abbench." + module), function)(*args, **kwargs)


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def _deadline(options):
    if options.get("deadline_utc") is None:
        return math.inf
    value = datetime.fromisoformat(options["deadline_utc"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("deadline_utc must include a timezone")
    return time.monotonic() + (value - datetime.now(timezone.utc)).total_seconds()


def _options(value):
    if value is not None and not isinstance(value, dict):
        raise ValueError("options must be an object")
    result = dict(value or {})
    nonnegative = {"wait_after_boot_s", "recovery_s", "cooling_wait_s", "minimum_suite_budget_s"}
    positive = {"screen_on_idle_s", "screen_off_standby_s", "cpu_timeout_s", "gpu_timeout_s",
                "persist_timeout_s", "geekbench_poll_interval_s", "adapter_timeout_s", "max_start_temperature_c"}
    for name in nonnegative | positive | {"temperature_baseline_c"}:
        if name not in result or (name == "temperature_baseline_c" and result[name] is None):
            continue
        number = result[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError(name + " must be finite numeric")
        if (name in nonnegative and number < 0) or (name in positive and number <= 0):
            raise ValueError(name + " is outside its allowed range")
    if "reboot" in result and not isinstance(result["reboot"], bool):
        raise ValueError("reboot must be boolean")
    if "boot_repeats" in result and (type(result["boot_repeats"]) is not int or not 1 <= result["boot_repeats"] <= 5):
        raise ValueError("boot_repeats must be 1..5")
    return result


def _path(value):
    if not isinstance(value, str) or not value.startswith("/") or ".." in value.split("/") or any(c in value for c in "\n\r\0"):
        raise ValueError("identity paths must be explicit absolute paths")
    return shlex.quote(value)


def read_identity(serial, directory, profile):
    """只读实物镜像、运行内核标识与指纹；路径来自设备配置。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    config = profile.get("identity_readback", {})
    if config.get("kernel_runtime_kind") not in ("sha256", "notes_sha256"):
        raise ValueError("kernel_runtime_kind must explicitly identify sha256")
    image, runtime = _path(config.get("image_path")), _path(config.get("kernel_runtime_path"))
    before = _call("capture", "clock_probe", serial)
    command = ("set -e; printf 'image_sha256='; sha256sum " + image +
               "; printf 'kernel_runtime_id='; sha256sum " + runtime +
               "; printf 'fingerprint='; getprop ro.build.fingerprint; "
               "printf 'cmdline='; cat /proc/cmdline; printf 'slot='; getprop ro.boot.slot_suffix")
    proc = _call("capture", "shell", serial, command, timeout=30)
    (target / "identity.stdout.txt").write_text(proc.stdout)
    (target / "identity.stderr.txt").write_text(proc.stderr)
    after = _call("capture", "clock_probe", serial)
    fields = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in fields:
                raise ValueError("duplicate_identity_field")
            fields[key] = value.strip()
    for name in ("image_sha256", "kernel_runtime_id"):
        fields[name] = fields.get(name, "").split()[0] if fields.get(name) else ""
        if not re.fullmatch(r"[0-9a-f]{64}", fields[name]):
            raise ValueError("invalid_readback_" + name)
    if before["boot_id"] != after["boot_id"]:
        raise ValueError("identity_crossed_boot")
    fields.update(boot_id=before["boot_id"], valid=True,
                  method="sha256_configured_image_and_kernel_runtime_paths_plus_runtime_properties",
                  source=str(target / "identity.stdout.txt"))
    _write(target / "identity.json", fields)
    return fields


def verify_identity(identity, manifest, mode, profile):
    expected = manifest["images"][mode]
    issues = []
    for actual, planned in (("image_sha256", "image_sha256"), ("kernel_runtime_id", "kernel_runtime_id"),
                            ("fingerprint", "android_fingerprint")):
        if identity.get(actual) != expected.get(planned):
            issues.append("identity_mismatch_" + actual)
    config = profile.get("identity_readback", {})
    if identity.get("slot") != config.get("slot_suffix") or config.get("slot_suffix") not in ("_a", "_b"):
        issues.append("active_slot_readback_mismatch_or_unconfigured")
    if config.get("mode_evidence_source") != "cmdline":
        issues.append("mode_evidence_source_unsupported")
    token = config.get("mode_evidence_token", {}).get(mode)
    xhyper_token = config.get("mode_evidence_token", {}).get("xhyper")
    if mode == "xhyper" and (not isinstance(token, str) or not token.strip()):
        issues.append("xhyper_positive_runtime_evidence_unconfigured")
    if mode == "native" and isinstance(xhyper_token, str) and xhyper_token in identity.get("cmdline", "").split():
        issues.append("native_has_xhyper_runtime_token")
    if token is not None and (not isinstance(token, str) or token not in identity.get("cmdline", "").split()):
        issues.append("mode_runtime_token_missing")
    return {"valid": not issues, "issues": issues, "method": "manifest_contract_and_device_image_runtime_readback",
            "mode_evidence": expected.get("mode_evidence"), "readback": identity,
            "native_absent_token_is_not_standalone_proof": mode == "native" and token is None}


def _score_rows(result):
    record = result.get("result", {})
    for name, value in record.get("metrics", {}).items():
        yield "geekbench_" + result["kind"] + "_" + name, value
    for section_index, section in enumerate(record.get("document", {}).get("sections", [])):
        if not isinstance(section, dict):
            continue
        section_name = section.get("name", str(section_index))
        if "score" in section:
            yield "geekbench_" + result["kind"] + "_section_" + section_name, section["score"]
        for index, workload in enumerate(section.get("workloads", [])):
            if isinstance(workload, dict) and "score" in workload:
                yield "geekbench_" + result["kind"] + "_" + section_name + "_" + workload.get("name", str(index)), workload["score"]


@serialized
def run_suite(serial, directory, mode, processor, profile, *, validation_only=False, manifest=None, options=None):
    """单模式全覆盖入口；成功采集、正式身份、计量资格分别保存。"""
    if mode not in ("native", "xhyper") or not isinstance(profile, dict) or not isinstance(validation_only, bool):
        raise ValueError("invalid suite mode/profile/purpose")
    options = _options(options)
    deadline = _deadline(options)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    result = {"serial": serial, "mode": mode, "purpose": "validation" if validation_only else "formal",
              "validation_only": validation_only, "valid": False, "identity_verified": False,
              "boot_id": None, "steps": [], "metric_rows": [], "failed_runs": [], "skipped": [],
              "temperature_baseline_c": options.get("temperature_baseline_c"),
              "pairing_verified": options.get("temperature_baseline_c") is not None,
              "comparison_settings": {}, "settings_verified": False,
              "requires_device_idle_confirmation": False,
              "limitations": ["单模式套件不能自行构成双侧对比", "USB电池净变化不是整机功耗",
                              "累计待机端点包含终点查询及唤醒开销"]}

    def save():
        _write(target / "suite.json", result)

    def remaining():
        return deadline - time.monotonic()

    def step(name, budget, function):
        if remaining() < budget:
            item = {"name": name, "status": "skipped", "reason": "deadline_insufficient_budget", "required_s": budget}
            result["steps"].append(item)
            result["skipped"].append(item)
            save()
            return None
        item = {"name": name, "status": "running", "host_start_s": time.monotonic()}
        first_row = len(result["metric_rows"])
        result["steps"].append(item)
        save()
        try:
            value = function()
            item.update(result=value, status="completed", host_end_s=time.monotonic())
            if remaining() <= 0:
                raise ValueError("step_completed_after_deadline")
            if isinstance(value, dict) and value.get("valid") is False:
                raise ValueError(value.get("reason") or "step_invalid")
            save()
            return value
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            item.update(status="failed", reason=str(exc), host_end_s=time.monotonic())
            result["failed_runs"].append({"name": name, "reason": str(exc)})
            if remaining() <= 0:
                for entry in result["metric_rows"][first_row:]:
                    entry.update(valid=False, reason=entry.get("reason") or str(exc), stage_failure=str(exc))
            save()
            return None

    def row(metric, value, run_id, valid=True, reason=None, **extra):
        finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        entry = {"mode": mode, "metric": metric, "value": value, "boot_id": extra.pop("boot_id", result["boot_id"]),
                 "run_id": run_id, "valid": valid and finite, "reason": reason,
                 "purpose": result["purpose"],
                 "validation_only": validation_only, "image_sha256": result.get("identity", {}).get("readback", {}).get("image_sha256"),
                 "configuration": {"purpose": result["purpose"], "settings": result["comparison_settings"]}, **extra}
        result["metric_rows"].append(entry)

    def analyze(directory, start, end, workload, run_id, valid=True,
                boundary="explicit_workload_observation_window", workload_identity=None):
        data = _call("analysis", "analyze_capture", directory, processor, profile=profile, start_s=start, end_s=end)
        power = data.get("power", {})
        for metric, field, unit in (("mean_power_w", "mean_w", "W"), ("energy_j", "energy_j", "J")):
            row(workload + "_" + metric, power.get(field), run_id, valid and power.get("valid") is True,
                power.get("reason"), unit=unit, power_boundary=power.get("power_boundary", "battery_net"),
                window_start_s=start, window_end_s=end, measurement_boundary=boundary,
                source=str(Path(directory) / "analysis/analysis.json"), **(workload_identity or {}))
        memory = data.get("memory", {})
        for field in ("mem_available_sampled_min_bytes", "estimated_unavailable_mean_bytes"):
            row(workload + "_" + field, memory.get(field), run_id, valid and memory.get("valid") is True,
                memory.get("reason"), unit="bytes", measurement_boundary=memory.get("measurement_boundary"),
                memory_window={key: value for key, value in memory.items() if key not in
                               ("mem_available_mean_bytes", "mem_available_sampled_min_bytes", "estimated_unavailable_mean_bytes")},
                source=str(Path(directory) / "analysis/analysis.json"), **(workload_identity or {}))
        return data

    try:
        if not validation_only:
            contract = _call("manifest", "validate_manifest", manifest)
            result["manifest_validation"] = contract
            if not contract["valid"]:
                raise ValueError("formal_manifest_invalid:" + ",".join(contract["issues"]))
        reboot = options.get("reboot", False)
        boot_repeats = options.get("boot_repeats", 1)
        if not isinstance(reboot, bool) or type(boot_repeats) is not int or not 1 <= boot_repeats <= 5:
            raise ValueError("invalid boot options")
        if reboot:
            for index in range(boot_repeats):
                print("即将对 " + serial + " 执行正常重启，采集本次启动样本。", flush=True)
                boot = step(f"boot-{index + 1}", 180, lambda i=index: _call("boot", "measure_boot", serial,
                            target / f"boot-{i + 1}", timeout_s=180, reboot=True, desktop=True))
                if not boot:
                    raise ValueError("new_boot_not_verified")
                result["boot_id"] = boot["boot_id"]
                row("warm_reboot_to_system_complete_s", boot.get("request_to_system_complete_observed_s"),
                    target.name + f"-boot-{index + 1}", boot_id=boot["boot_id"], unit="s",
                    observation_interval_s=boot.get("request_to_system_complete_interval_s"),
                    measurement_boundary=boot["measurement_boundary"], source=str(target / f"boot-{index + 1}/boot.json"))
        else:
            result["skipped"].append({"name": "boot_duration", "reason": "reboot_not_requested_no_new_boot_sample"})
        environment = step("environment-before", 180, lambda: _call("environment", "capture_environment", serial,
                           target / "environment-before", expected_boot_id=result["boot_id"],
                           deadline_monotonic_s=None if math.isinf(deadline) else deadline))
        if not environment:
            raise ValueError("environment_unavailable")
        result["boot_id"] = environment["boot_id"]
        stable_s = options.get("wait_after_boot_s", profile.get("wait_after_boot_s", 300))
        if not validation_only:
            stable_s = max(300, stable_s)
        wait = max(0, stable_s - environment["uptime_s"])
        needed_stability_wait = wait > 0
        if remaining() < wait + 30:
            raise ValueError("deadline_before_boot_stability")
        while wait > 0:
            if remaining() <= 12:
                raise ValueError("deadline_during_boot_stability")
            time.sleep(min(30, wait, remaining() - 12))
            probe = _call("capture", "clock_probe", serial)
            if remaining() <= 0:
                raise ValueError("deadline_during_boot_stability")
            if probe["boot_id"] != result["boot_id"]:
                raise ValueError("boot_changed_during_stability")
            wait = max(0, stable_s - probe["t_s"])
        if needed_stability_wait:
            environment = step("environment-stable", 180, lambda: _call("environment", "capture_environment", serial,
                               target / "environment-stable", expected_boot_id=result["boot_id"],
                               deadline_monotonic_s=None if math.isinf(deadline) else deadline))
            if not environment:
                raise ValueError("stable_environment_unavailable")
        result["comparison_settings"] = environment.get("settings", {})
        required_settings = ("screen_brightness", "screen_brightness_mode", "screen_off_timeout", "stay_on_while_plugged_in")
        missing_required = [key for key in required_settings if result["comparison_settings"].get(key) is None]
        result["settings_unverified_fields"] = sorted(set(missing_required +
            [key for key, value in result["comparison_settings"].items() if value is None]))
        expected_settings = options.get("settings_baseline")
        result["settings_verified"] = not missing_required and (
            expected_settings is None or result["comparison_settings"] == expected_settings)
        if not validation_only and not result["settings_verified"]:
            raise ValueError("formal_settings_unavailable_or_pair_mismatch")
        if not validation_only:
            identity = step("identity-readback", 60, lambda: read_identity(serial, target / "identity", profile))
            if not identity or identity["boot_id"] != result["boot_id"]:
                raise ValueError("identity_unavailable_or_cross_boot")
            result["identity"] = verify_identity(identity, manifest, mode, profile)
            if not result["identity"]["valid"]:
                raise ValueError(",".join(result["identity"]["issues"]))
            result["identity_verified"] = True
        if result["temperature_baseline_c"] is None:
            result["temperature_baseline_c"] = environment.get("battery_temperature_c")
        memory = step("memory-baseline", 120, lambda: _call("environment", "capture_memory_baseline", serial,
                      target / "memory-baseline", count=3, interval_s=10, expected_boot_id=result["boot_id"],
                      include_process_meminfo=True, deadline_monotonic_s=None if math.isinf(deadline) else deadline))
        if memory:
            if memory["boot_id"] != result["boot_id"]:
                raise ValueError("memory_baseline_cross_boot")
            for field, metric in (("mem_total_bytes", "memory_baseline_visible_total_bytes"),
                                  ("mean_mem_available_bytes", "memory_baseline_available_bytes"),
                                  ("mean_estimated_unavailable_bytes", "memory_baseline_estimated_unavailable_bytes")):
                row(metric, memory["aggregate"].get(field), target.name + "-memory-baseline", unit="bytes",
                    measurement_boundary="android_kernel_visible_memory", source=str(target / "memory-baseline"))
        for kind, default_s in (("screen_on_idle", 480), ("screen_off_standby", 900)):
            duration = options.get(kind + "_s", 10 if validation_only else default_s)
            idle = step(kind, duration + 180, lambda k=kind, d=duration: _call("idle", "run_idle", serial,
                        target / k, kind=k, duration_s=d, mode=mode, power_config=profile,
                        prepare_keyguard=True,
                        battery_path=profile.get("battery_path", "/sys/class/power_supply/battery"),
                        usb_online_path=profile.get("external_supply_path", "/sys/class/power_supply/usb/online")))
            if idle:
                if idle["boot_id"] != result["boot_id"]:
                    raise ValueError("idle_cross_boot")
                if kind == "screen_on_idle":
                    start, end = idle["capture_window_start"]["t_s"], idle["capture_window_end"]["t_s"]
                    step(kind + "-analysis", 60, lambda: analyze(target / kind / "capture", start, end, kind, target.name + "-" + kind))
                else:
                    power = idle["power"]
                    for metric, field, unit in (("mean_power_w", "mean_w", "W"), ("energy_j", "energy_j", "J")):
                        row(idle["kind_observed"] + "_" + metric, power.get(field), target.name + "-" + kind,
                            power.get("valid") is True, power.get("reason"), unit=unit,
                            power_boundary=power.get("power_boundary", "battery_net"),
                            measurement_boundary="endpoint_window_including_end_query_and_wakeup", source=str(target / kind / "idle.json"))
        seen = set(options.get("seen_uuids", []))
        for kind in ("cpu", "gpu"):
            compute_s = options.get(kind + "_timeout_s", 1200)
            persist_s = options.get("persist_timeout_s", 180)

            def benchmark(k=kind):
                recovery = options.get("recovery_s", 60)
                if not validation_only:
                    recovery = max(60, recovery)
                time.sleep(min(recovery, max(0, remaining())))
                cooling_start, cooling_index = time.monotonic(), 0
                cooling_limit = min(600, options.get("cooling_wait_s", 600))
                while True:
                    env = _call("environment", "capture_environment", serial, target / (k + f"-environment-{cooling_index}"),
                                expected_boot_id=result["boot_id"], deadline_monotonic_s=None if math.isinf(deadline) else deadline)
                    if not env.get("valid"):
                        raise ValueError("benchmark_environment_invalid:" + str(env.get("reason")))
                    thermal = _call("environment", "thermal_gate", env, baseline_temperature_c=result["temperature_baseline_c"],
                                    max_start_c=options.get("max_start_temperature_c", 40.0),
                                    max_delta_c=profile.get("battery_start_temperature_pair_tolerance_c", 1.5))
                    if thermal["valid"] or time.monotonic() - cooling_start >= cooling_limit or remaining() < compute_s + persist_s + 120:
                        break
                    time.sleep(min(30, cooling_limit - (time.monotonic() - cooling_start)))
                    cooling_index += 1
                result[k + "_thermal"] = thermal
                if not thermal["valid"]:
                    raise ValueError("thermal_gate:" + str(thermal["reason"]))
                if not validation_only and env.get("settings") != result["comparison_settings"]:
                    raise ValueError("settings_not_restored_or_changed_before_workload")
                if remaining() < compute_s + persist_s + 180:
                    raise ValueError("deadline_after_cooling_before_workload")
                if compute_s + persist_s + 240 > 1800:
                    raise ValueError("capture_limit_cannot_cover_allowed_workload_lifecycle")
                prepared_ui = _call("ui_control", "prepare_ui", serial, target / (k + "-ui-preparation"),
                                    expected_boot_id=result["boot_id"],
                                    deadline_monotonic_s=None if math.isinf(deadline) else deadline)
                if not prepared_ui.get("valid"):
                    raise ValueError("ui_preparation_failed:" + str(prepared_ui.get("reason")))
                capture_dir = target / (k + "-telemetry")
                armed = False
                run, workload_error = None, None
                try:
                    _call("capture", "start_capture", serial, capture_dir,
                          duration_s=min(1800, compute_s + persist_s + 240), mode=mode)
                    armed = True
                    time.sleep(2)
                    result["requires_device_idle_confirmation"] = True
                    run = _call("geekbench_runner", "run_geekbench", serial, k, target / k, mode=mode,
                                api=options.get("gpu_api", "Vulkan"), expected_boot_id=result["boot_id"],
                                expected_app_version=(manifest["images"][mode]["app_version"] if not validation_only else
                                                      profile.get("expected_app_version_from_preflight", "6.7.1")),
                                expected_apk_sha256=None if validation_only else manifest["images"][mode]["app_apk_sha256"],
                                compute_timeout_s=compute_s, persist_timeout_s=persist_s,
                                poll_interval_s=options.get("geekbench_poll_interval_s", 60), seen_uuids=seen)
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    workload_error = str(exc)
                finally:
                    if armed:
                        time.sleep(2)
                        _call("capture", "stop_capture", capture_dir)
                if workload_error or not run or not run["valid"]:
                    analyze(capture_dir, None, None, "geekbench_" + k, target.name + "-" + k,
                            False, boundary="failed_workload_capture_lifecycle")
                    raise ValueError("benchmark_failed:" + str(workload_error or run.get("reason")))
                result["requires_device_idle_confirmation"] = False
                seen.add(run["result_uuid"])
                eligible = k != "gpu" or run.get("gpu_hardware_verified") is True
                reason = None if eligible else "gpu_hardware_identity_unverified"
                if not validation_only and run["app"].get("apk_sha256") != manifest["images"][mode]["app_apk_sha256"]:
                    eligible, reason = False, "app_apk_identity_mismatch"
                observer = {"method": run.get("observer"),
                            "poll_interval_s": run.get("poll_interval_s", options.get("geekbench_poll_interval_s", 60)),
                            "ui_max_attempts": 3, "telemetry": "perfetto_power_memory_v1"}
                for metric, value in _score_rows(run):
                    row(metric, value, target.name + "-" + k, eligible, reason, unit="score",
                        version=run["app"]["version"], apk_sha256=run["app"].get("apk_sha256"),
                        api=run.get("requested_api"), api_raw=run.get("result", {}).get("api_raw"),
                        uuid=run["result_uuid"], observer=observer, source=str(target / k / "result.json"))
                row("geekbench_" + k + "_internal_runtime_s", run.get("internal_runtime_s"),
                    target.name + "-" + k, eligible, reason, unit="s", observer=observer,
                    version=run["app"]["version"], apk_sha256=run["app"].get("apk_sha256"),
                    api=run.get("requested_api"), api_raw=run.get("result", {}).get("api_raw"),
                    measurement_boundary="geekbench_reported_internal_runtime",
                    uuid=run["result_uuid"], source=str(target / k / "result.json"))
                start = run["start_request_window"]["before_s"]
                window = run.get("compute_complete_window")
                reliable = (run.get("compute_complete_observed") is True and window is not None
                            and window["lower_s"] > run["start_request_window"]["after_s"]
                            and 0 <= window["upper_s"] - window["lower_s"] <= 120)
                persisted = run.get("persisted_window")
                end = window["upper_s"] if reliable else (persisted["after_s"] if persisted else run.get("end_boottime_s"))
                if end is None:
                    raise ValueError("benchmark_end_boundary_unavailable")
                boundary = ("click_to_compute_complete_observed_upper" if reliable else
                            "click_to_result_persisted_observed_lifecycle" if persisted else
                            "click_to_result_exported_observed_lifecycle")
                result[k + "_window"] = {"start_s": start, "end_s": end,
                    "boundary": boundary,
                    "compute_end_interval": window, "precise_compute_window": False}
                result[k + "_observer"] = {"method": run.get("observer"), "poll_interval_s": run.get("poll_interval_s"),
                    "overhead_quantified": False, "note": "界面观察存在开销，双方必须使用同一观察设置。"}
                workload_identity = {"version": run["app"]["version"], "apk_sha256": run["app"].get("apk_sha256"),
                                     "api": run.get("requested_api"), "api_raw": run.get("result", {}).get("api_raw"),
                                     "observer": observer, "uuid": run["result_uuid"]}
                data = analyze(capture_dir, start, end, "geekbench_" + k, target.name + "-" + k, eligible,
                               boundary, workload_identity)
                time.sleep(min(recovery, max(0, remaining())))
                after_memory = _call("environment", "capture_memory_baseline", serial, target / (k + "-memory-after"),
                      count=1, interval_s=10, expected_boot_id=result["boot_id"],
                      deadline_monotonic_s=None if math.isinf(deadline) else deadline)
                for field, metric in (("mean_mem_available_bytes", "available_bytes"),
                                      ("mean_estimated_unavailable_bytes", "estimated_unavailable_bytes")):
                    row("geekbench_" + k + "_memory_after_" + metric,
                        (after_memory.get("aggregate") or {}).get(field), target.name + "-" + k,
                        eligible and after_memory.get("valid") is True, after_memory.get("reason"), unit="bytes",
                        measurement_boundary="android_kernel_visible_memory_after_recovery",
                        source=str(target / (k + "-memory-after")), **workload_identity)
                return {"valid": eligible, "reason": reason, "run": run, "analysis": data}

            completed = step("geekbench-" + kind, compute_s + persist_s + 300, benchmark)
            if completed is None and kind == "cpu":
                result["skipped"].append({"name": "geekbench-gpu", "reason": "previous_workload_or_capture_failed_requires_idle_confirmation"})
                break
        result["valid"] = not result["failed_runs"] and not any(
            item.get("reason") != "reboot_not_requested_no_new_boot_sample" for item in result["skipped"])
    except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        result["failed_runs"].append({"name": "suite", "reason": "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)})
        result["valid"] = False
    finally:
        if not validation_only and not result["identity_verified"]:
            for entry in result["metric_rows"]:
                entry.update(valid=False, reason=entry.get("reason") or "formal_identity_unverified")
        save()
        campaign = {"purpose": result["purpose"], "identity_verified": result["identity_verified"],
                    "failed_runs": result["failed_runs"], "skipped": result["skipped"],
                    "pairing_verified": result["pairing_verified"], "single_mode": mode,
                    "limitations": result["limitations"]}
        result["report"] = _call("report", "create_report", result["metric_rows"], target / "report", campaign=campaign)
        save()
    return result


@serialized
def run_campaign(serial, directory, processor, profile, *, manifest, adapter, repetitions=1, validation_only=False, options=None):
    """先覆盖两侧，再增加重复；切换适配器以 argv 运行，绝不使用 shell。"""
    if not isinstance(adapter, list) or not adapter or not all(isinstance(arg, str) and arg for arg in adapter):
        raise ValueError("automatic AB requires an explicit adapter argv list")
    if type(repetitions) is not int or not 1 <= repetitions <= 5:
        raise ValueError("repetitions must be 1..5")
    options = _options(options)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    deadline = _deadline(options)
    result = {"purpose": "validation" if validation_only else "formal", "suites": [], "metric_rows": [],
              "failed_runs": [], "skipped": [], "repetitions_requested": repetitions, "valid": False,
              "minimum_boot_target_each_mode": 3, "adapter": adapter}
    if not validation_only:
        contract = _call("manifest", "validate_manifest", manifest)
        result["manifest_validation"] = contract
        if not contract["valid"]:
            result["failed_runs"].append({"name": "campaign_preflight",
                "reason": "formal_manifest_invalid:" + ",".join(contract["issues"])})
            result["report"] = _call("report", "create_report", [], target / "report", campaign=result)
            _write(target / "campaign.json", result)
            return result
    baseline = options.get("temperature_baseline_c")
    settings_baseline = options.get("settings_baseline")
    interrupted = False
    halt_reason = None
    for repetition in range(repetitions):
        if interrupted:
            break
        for mode in ("native", "xhyper"):
            name = f"{repetition + 1:02d}-{mode}"
            remaining = deadline - time.monotonic()
            recovery = options.get("recovery_s", 60) if validation_only else max(60, options.get("recovery_s", 60))
            boots = options.get("boot_repeats", 3 if repetition == 0 else 1) if options.get("reboot", not validation_only) else 0
            stability = 0 if validation_only else max(300, options.get("wait_after_boot_s", profile.get("wait_after_boot_s", 300)))
            estimated = (options.get("screen_on_idle_s", 10 if validation_only else 480) +
                         options.get("screen_off_standby_s", 10 if validation_only else 900) +
                         options.get("cpu_timeout_s", 1200) + options.get("gpu_timeout_s", 1200) +
                         2 * options.get("persist_timeout_s", 180) + 4 * recovery +
                         2 * min(600, options.get("cooling_wait_s", 600)) + 1320 +
                         boots * 180 + stability)
            if remaining <= max(options.get("minimum_suite_budget_s", estimated), estimated):
                result["skipped"].append({"name": name, "reason": "deadline_insufficient_suite_coverage_budget"})
                continue
            try:
                try:
                    argv = [arg.format(mode=mode, serial=serial, out=str(target / name)) for arg in adapter]
                except (KeyError, IndexError, ValueError) as exc:
                    raise ValueError("invalid_adapter_placeholder") from exc
                previous_boot = _call("capture", "clock_probe", serial)["boot_id"]
                proc = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                      timeout=min(options.get("adapter_timeout_s", 300), remaining))
                (target / (name + "-adapter.stdout.txt")).write_text(proc.stdout)
                (target / (name + "-adapter.stderr.txt")).write_text(proc.stderr)
                if proc.returncode != 0 or time.monotonic() >= deadline:
                    raise ValueError("mode_adapter_failed_or_late")
                current_boot = _call("capture", "clock_probe", serial)["boot_id"]
                if previous_boot == current_boot:
                    raise ValueError("mode_adapter_did_not_establish_new_boot")
                suite_options = {**options, "temperature_baseline_c": baseline, "settings_baseline": settings_baseline}
                if not validation_only:
                    suite_options.setdefault("reboot", True)
                    suite_options.setdefault("boot_repeats", 3 if repetition == 0 else 1)
                suite = run_suite(serial, target / name, mode, processor, profile,
                                  validation_only=validation_only, manifest=manifest, options=suite_options)
                if baseline is None:
                    baseline = suite.get("temperature_baseline_c")
                if settings_baseline is None:
                    settings_baseline = suite.get("comparison_settings")
                result["suites"].append({"name": name, "mode": mode, "valid": suite["valid"], "directory": str(target / name)})
                result["metric_rows"].extend(suite["metric_rows"])
                result["failed_runs"].extend(suite["failed_runs"])
                result["skipped"].extend(suite["skipped"])
                if any(item.get("reason") == "interrupted" for item in suite["failed_runs"]):
                    interrupted = True
                    halt_reason = "interrupted_no_more_mode_switches"
                if suite.get("requires_device_idle_confirmation") is True:
                    interrupted = True
                    halt_reason = "workload_or_capture_state_unconfirmed_no_more_mode_switches"
            except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    interrupted = True
                    halt_reason = "interrupted_no_more_mode_switches"
                for channel in ("stdout", "stderr"):
                    raw = getattr(exc, channel, None)
                    if raw is not None:
                        text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
                        (target / (name + "-adapter." + channel + ".txt")).write_text(text)
                result["failed_runs"].append({"name": name, "reason": "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)})
            _write(target / "campaign.json", result)
            if interrupted:
                result["skipped"].append({"name": "remaining_campaign", "reason": halt_reason})
                break
    boot_counts = {mode: len({row["boot_id"] for row in result["metric_rows"] if row["mode"] == mode and
                              row["metric"] == "warm_reboot_to_system_complete_s" and row["valid"]}) for mode in ("native", "xhyper")}
    result["distinct_measured_boots"] = boot_counts
    if not validation_only and any(count < 3 for count in boot_counts.values()):
        result["skipped"].append({"name": "minimum_boot_samples", "reason": "fewer_than_three_verified_new_boots_each_mode"})
    result["valid"] = not result["failed_runs"] and not result["skipped"]
    result["report"] = _call("report", "create_report", result["metric_rows"], target / "report", campaign=result)
    _write(target / "campaign.json", result)
    return result
