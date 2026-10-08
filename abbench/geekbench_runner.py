"""运行已安装的 Geekbench 安卓应用，绑定新结果并保存一致性数据库。"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .adb_io import pull_root
from .geekbench import export_database


PACKAGE = "com.primatelabs.geekbench6"
ACTIVITY = PACKAGE + "/com.primatelabs.geekbench.HomeActivity"
DATABASE = "/data/data/" + PACKAGE + "/files/history.db"


class RunnerError(ValueError):
    """运行或结果归属无法确认，原始证据应留在本次运行目录。"""


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def ui_nodes(text):
    """只解析本次有效 XML；不靠旧坐标或应用的历史结果数判断完成。"""
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<hierarchy")
    if start < 0:
        raise RunnerError("ui_xml_missing")
    try:
        return list(ET.fromstring(text[start:].strip()).iter("node"))
    except ET.ParseError as exc:
        raise RunnerError("ui_xml_invalid") from exc


def _center(node):
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match or node.get("enabled") == "false":
        raise RunnerError("ui_target_not_enabled_or_visible")
    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        raise RunnerError("ui_target_not_visible")
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def find_target(nodes, *, resource=None, label=None):
    matches = [node for node in nodes if
               (resource and node.get("resource-id", "").endswith(":id/" + resource)) or
               (label and (node.get("text", "").strip().casefold() == label.casefold() or
                           node.get("content-desc", "").strip().casefold() == label.casefold()))]
    if label and not resource:
        clickable = [node for node in matches if node.get("clickable") == "true"]
        if clickable:
            matches = clickable
    # Do not collapse multiple targets to the first arbitrary coordinate.
    if len(matches) != 1:
        raise RunnerError("ui_target_missing_or_ambiguous:" + str(resource or label))
    _center(matches[0])
    return matches[0]


def selected_api(nodes):
    spinner = find_target(nodes, resource="computeApiSpinner")
    values = {node.get("text", "").strip() for node in spinner.iter("node")
              if node.get("text", "").strip() in ("Vulkan", "OpenCL")}
    if len(values) != 1:
        raise RunnerError("gpu_api_selection_unverified")
    return values.pop()


def _computing_dialog(nodes):
    """识别本版实际工作负载弹窗，不能凭任意 Running 文本判定计算。"""
    for panel in nodes:
        if panel.get("resource-id") != "android:id/parentPanel" or panel.get("package") != PACKAGE:
            continue
        fields = {}
        for node in panel.iter("node"):
            if node.get("package") == PACKAGE:
                fields.setdefault(node.get("resource-id"), []).append(node)
        required = ("alertTitle", "message", "progress_percent", "button2")
        if any(len(fields.get("android:id/" + name, [])) != 1 for name in required):
            continue
        title, message, percentage, cancel = (fields["android:id/" + name][0] for name in required)
        percent_text = percentage.get("text", "").strip()
        if (title.get("text", "").strip() == "Geekbench 6"
                and re.fullmatch(r"Running\s+\S[^\n]*", message.get("text", "").strip())
                and re.fullmatch(r"(?:100|[1-9]?\d)%", percent_text)
                and cancel.get("text", "").strip().casefold() == "cancel"
                and cancel.get("enabled") != "false"):
            return True
    return False


def classify_ui(nodes):
    texts = [node.get("text", "").strip() for node in nodes]
    joined = "\n".join(texts)
    if re.search(r"upload.{0,30}(fail|error)|failed.{0,30}upload|上传失败|无法上传", joined, re.I):
        return "upload_failed"
    if re.search(r"benchmark (canceled|cancelled)|(?:CPU|GPU|Benchmark).{0,10} Error|基准.{0,10}(错误|取消)", joined, re.I):
        return "benchmark_failed"
    if re.search(r"uploading|正在上传|上传结果", joined, re.I):
        return "uploading"
    if _computing_dialog(nodes):
        return "computing"
    resources = {node.get("resource-id", "").split(":id/")[-1] for node in nodes}
    if "benchmarkWebView" in resources or "resultsFrame" in resources or re.search(r"(?:Vulkan|OpenCL|Single.Core|Multi.Core) Score", joined, re.I):
        return "result"
    if any(node.get("package") == PACKAGE and node.get("resource-id") in
           (PACKAGE + ":id/progress_current_workload", PACKAGE + ":id/progress_progress") for node in nodes):
        return "computing"
    if "runCpuBenchmarks" in resources or "runComputeBenchmark" in resources:
        return "home"
    return "unknown"


def _document_api(document):
    # String evidence from the result, never an invented compute_api integer mapping.
    values = [document.get("compute_platform_name", "")]
    values.extend(metric.get("value", "") for metric in document.get("metrics", []) if isinstance(metric, dict))
    values.extend(section.get("name", "") for section in document.get("sections", []) if isinstance(section, dict))
    found = {api for api in ("Vulkan", "OpenCL") if any(
        isinstance(value, str) and re.search(r"\b" + api + r"\b", value) for value in values)}
    return next(iter(found)) if len(found) == 1 else None


def _gpu_identity(document):
    """保留结果的设备/驱动身份；明确软件实现不可计作硬件 GPU。"""
    name = document.get("compute_device_name")
    platform = document.get("compute_platform_name")
    drivers = {key: value for key, value in document.items() if "driver" in key.lower()}
    metrics = document.get("metrics", [])
    if not isinstance(metrics, list):
        raise RunnerError("gpu_metrics_schema_invalid")
    metric_texts = [item.get("value", "") for item in metrics if isinstance(item, dict)]
    texts = [name, platform, *drivers.values(), *metric_texts]
    if any(isinstance(value, str) and re.search(
            r"swiftshader|llvmpipe|lavapipe|software\s+(?:renderer|device|rasterizer)", value, re.I)
           for value in texts):
        raise RunnerError("software_gpu_result_not_hardware")
    # This GPU family is supported by this tablet's saved result evidence. A
    # driver API string alone does not establish a hardware device identity.
    known_hardware = isinstance(name, str) and bool(re.match(r"^Mali[- ](?:G\d+|T\d+)\b", name, re.I))
    return {"device_name": name, "platform_name": platform, "driver_fields": drivers,
            "gpu_hardware_verified": known_hardware,
            "hardware_evidence": "result_compute_device_name_Mali_family" if known_hardware else None}


def bind_result(before, after, *, kind, expected_version, boot_id, end_boot_id,
                api=None, result_ui=None, seen_uuids=()):
    """严格绑定唯一新增文档、新 UUID、类型、版本和当前开机，保留原始 API。"""
    if boot_id != end_boot_id:
        raise RunnerError("cross_boot_result")
    previous = before.get("records", [])
    high_water = max((rec["document_id"] for rec in previous), default=0)
    previous_uuids = {rec.get("uuid") for rec in previous if rec.get("uuid")}
    previous_uuids.update(seen_uuids)
    candidates = [rec for rec in after.get("records", []) if rec["document_id"] > high_water]
    if len(candidates) != 1:
        raise RunnerError("new_result_missing" if not candidates else "multiple_new_results")
    record = candidates[0]
    if record.get("kind") != kind:
        raise RunnerError("new_result_kind_mismatch")
    if not isinstance(record.get("uuid"), str) or not record["uuid"] or record["uuid"] in previous_uuids:
        raise RunnerError("new_uuid_missing_or_reused")
    if not record.get("valid") or not record.get("complete"):
        raise RunnerError("new_result_invalid_or_incomplete:" + ",".join(record.get("exclusion_reasons", [])))
    version = str(record.get("version", "")).removeprefix("Geekbench ").strip()
    if version != expected_version:
        raise RunnerError("new_result_version_mismatch")
    evidence, gpu_identity = None, None
    if kind == "gpu":
        document = record["document"]
        gpu_identity = _gpu_identity(document)
        if document.get("compute_api") != record.get("api_raw"):
            raise RunnerError("gpu_raw_api_mismatch")
        evidence = _document_api(document)
        if evidence != api:
            raise RunnerError("gpu_api_result_mismatch_or_unverified")
    return {**record, "api_name_verified": evidence, "boot_id": boot_id,
            "gpu_identity": gpu_identity,
            "gpu_hardware_verified": gpu_identity["gpu_hardware_verified"] if gpu_identity else None,
            "binding": "new_document_id_and_uuid_after_baseline", "previous_document_id": high_water}


class _Device:
    def __init__(self, serial, out, command_timeout_s):
        self.serial, self.out = serial, Path(out)
        self.timeout = command_timeout_s
        self.index = 0
        self.remote = "/data/local/tmp/abbench-gb6-" + uuid.uuid4().hex
        self.sqlite_available = None
        self.deadline = None
        self.deadline_reason = "benchmark_stage_timeout"
        (self.out / "commands").mkdir()

    def shell(self, command, label):
        self.index += 1
        prefix = self.out / "commands" / f"{self.index:04d}-{label}"
        args = ["adb", "-s", self.serial, "shell", "su", "0", "sh", "-c", shlex.quote(command)]
        started = time.monotonic()
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline - started
            if remaining <= 0:
                raise RunnerError(self.deadline_reason)
            timeout = min(timeout, remaining)
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            stdout = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
            stderr = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
            returncode = None
        prefix.with_suffix(".stdout.txt").write_text(stdout, encoding="utf-8")
        prefix.with_suffix(".stderr.txt").write_text(stderr, encoding="utf-8")
        ended = time.monotonic()
        _write(prefix.with_suffix(".json"), {"command": command, "returncode": returncode,
                                            "timeout_s": timeout, "host_duration_s": ended - started})
        if self.deadline is not None and ended >= self.deadline:
            raise RunnerError(self.deadline_reason)
        if returncode != 0:
            raise RunnerError(f"device_command_failed:{label}:{returncode}:{stderr.strip()}")
        return stdout.replace("\r\n", "\n")

    def timed(self, command, label):
        script = ("set -e; printf 'AB_BOOT\\n'; cat /proc/sys/kernel/random/boot_id; "
                  "printf 'AB_BEFORE\\n'; cat /proc/uptime; " + command +
                  "; printf '\\nAB_AFTER\\n'; cat /proc/uptime")
        raw = self.shell(script, label)
        match = re.search(r"AB_BOOT\n([^\n]+)\nAB_BEFORE\n([\d.]+)[^\n]*\n(.*?)\nAB_AFTER\n([\d.]+)", raw, re.S)
        if not match:
            raise RunnerError("device_timing_missing:" + label)
        boot, before, payload, after = match.groups()
        before, after = float(before), float(after)
        if after < before:
            raise RunnerError("device_timing_reversed")
        return {"boot_id": boot.strip(), "before_s": before, "after_s": after, "payload": payload}

    def identity(self):
        return self.timed("true", "identity")

    def metadata(self):
        raw = self.shell("dumpsys package " + PACKAGE, "package")
        match = re.search(r"versionName=([^\s]+)", raw)
        if not match:
            raise RunnerError("geekbench_app_version_missing")
        paths = self.shell("pm path " + PACKAGE, "apk-paths").splitlines()
        if not paths or any(not path.startswith("package:/") for path in paths):
            raise RunnerError("geekbench_installed_apk_paths_missing_or_invalid")
        hashes = self.shell("sha256sum " + " ".join(shlex.quote(path.removeprefix("package:")) for path in paths), "apk-hashes")
        apk_files = []
        for line in hashes.splitlines():
            match_hash = re.fullmatch(r"([0-9a-f]{64})\s+\*?(.+)", line)
            if not match_hash:
                raise RunnerError("geekbench_apk_hash_invalid")
            apk_files.append({"name": Path(match_hash.group(2)).name, "sha256": match_hash.group(1)})
        if len(apk_files) != len(paths) or len({item["name"] for item in apk_files}) != len(paths):
            raise RunnerError("geekbench_apk_hash_set_incomplete_or_duplicated")
        apk_files.sort(key=lambda item: item["name"])
        apk_sha256 = hashlib.sha256(json.dumps(apk_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.sqlite_available = bool(self.shell("command -v sqlite3 || true", "sqlite-capability").strip())
        self.shell("mkdir -p " + shlex.quote(self.remote), "prepare-private-output")
        return {"package": PACKAGE, "version": match.group(1), "apk_hashes_raw": hashes,
                "apk_files": apk_files, "apk_sha256": apk_sha256,
                "sqlite_available": self.sqlite_available, "remote_directory": self.remote}

    def observe(self, label="ui"):
        # The platform may kill a dump during a window transition. Retry a new
        # snapshot only; never reuse the previous XML or repeat a benchmark tap.
        for attempt in range(3):
            remote = self.remote + f"/ui-{self.index + 1}-{attempt}.xml"
            observation = self.timed("uiautomator dump " + shlex.quote(remote) + " >/dev/null && cat " + shlex.quote(remote), label + f"-{attempt}")
            try:
                nodes = ui_nodes(observation["payload"])
            except RunnerError as exc:
                if str(exc) not in ("ui_xml_missing", "ui_xml_invalid") or attempt == 2:
                    raise
                remaining = 1 if self.deadline is None else self.deadline - time.monotonic()
                if remaining <= 0:
                    raise RunnerError(self.deadline_reason) from exc
                time.sleep(min(1, remaining))
                continue
            return {**observation, "nodes": nodes, "state": classify_ui(nodes)}

    def tap(self, node, label):
        x, y = _center(node)
        return self.timed(f"input tap {x} {y}", label)

    def start(self, kind, api):
        # No service changes, wakeup loop, temperature/frequency policy or image writes.
        self.shell("am start -W -n " + ACTIVITY, "launch")
        time.sleep(2)
        observation = self.observe("home")
        if observation["state"] in ("computing", "uploading"):
            raise RunnerError("another_geekbench_run_active")
        if observation["state"] == "result":
            self.timed("input keyevent KEYCODE_BACK", "leave-previous-result")
            observation = self.observe("home-after-back")
        button = "runCpuBenchmarks" if kind == "cpu" else "runComputeBenchmark"
        try:
            target = find_target(observation["nodes"], resource=button)
        except RunnerError:
            self.tap(find_target(observation["nodes"], label="CPU" if kind == "cpu" else "GPU"), "select-kind")
            observation = self.observe("kind-selected")
            target = find_target(observation["nodes"], resource=button)
        if kind == "gpu":
            if selected_api(observation["nodes"]) != api:
                self.tap(find_target(observation["nodes"], resource="computeApiSpinner"), "open-api-selector")
                menu = self.observe("api-menu")
                self.tap(find_target(menu["nodes"], label=api), "select-api")
                observation = self.observe("api-selected")
            if selected_api(observation["nodes"]) != api:
                raise RunnerError("gpu_requested_api_unavailable")
            target = find_target(observation["nodes"], resource=button)
        return self.tap(target, "start-benchmark")

    def new_document_id(self, baseline):
        if not self.sqlite_available:
            return None
        # An inserted document ID is a candidate, not proof that its result is complete.
        command = "sqlite3 -readonly " + shlex.quote(DATABASE) + " " + shlex.quote("SELECT coalesce(max(id),0) FROM documents;")
        observation = self.timed(command, "persist-check")
        try:
            current = int(observation["payload"].strip())
        except ValueError as exc:
            raise RunnerError("invalid_persistence_query") from exc
        if current <= baseline:
            return {**observation, "new": False}
        document = self.timed("sqlite3 -readonly " + shlex.quote(DATABASE) + " " + shlex.quote(
            "SELECT json FROM documents WHERE id=" + str(current) + ";"), "candidate-result-json")
        try:
            payload = json.loads(document["payload"])
        except (ValueError, TypeError) as exc:
            raise RunnerError("invalid_candidate_result_json") from exc
        ready = isinstance(payload, dict) and payload.get("complete_benchmark") in (True, 1)
        return {**document, "new": ready, "candidate_document_id": current}

    def snapshot(self, name, *, allow_stop=False):
        remote = self.remote + "/" + name
        self.shell("mkdir " + shlex.quote(remote), "prepare-" + name)
        if self.sqlite_available:
            backup = ".backup " + shlex.quote(remote + "/history.db")
            self.shell("sqlite3 -readonly " + shlex.quote(DATABASE) + " " + shlex.quote(backup), "backup-" + name)
            method = "sqlite_online_backup"
        else:
            if not allow_stop:
                raise RunnerError("consistent_snapshot_requires_idle_or_persisted_state")
            self.shell("am force-stop " + PACKAGE, "stop-only-geekbench-" + name)
            self.shell("set -e; for f in history.db history.db-wal history.db-shm; do "
                       "p=" + shlex.quote(DATABASE.rsplit("/", 1)[0]) + "/$f; "
                       "if [ -f \"$p\" ]; then cp \"$p\" " + shlex.quote(remote) + "/$f; fi; done", "copy-" + name)
            method = "stopped_geekbench_copy_with_wal_shm"
        target = self.out / name
        pull_root(self.serial, remote, target, timeout_s=self.timeout)
        exported = export_database(target / "history.db")
        _write(target / "export.json", exported)
        return exported, {"method": method, "directory": str(target), "integrity_checked": True}

    def failure_evidence(self):
        try:
            self.observe("failure-ui")
        except (ValueError, OSError):
            pass
        try:
            self.shell("logcat -b main -d -t 200 -s Geekbench:* geekbench:* AndroidRuntime:E", "failure-logcat")
        except (ValueError, OSError):
            pass


def run_geekbench(serial, kind, out, *, mode=None, api="Vulkan", expected_app_version="6.7.1",
                  expected_boot_id=None, poll_interval_s=30, compute_timeout_s=1200,
                  persist_timeout_s=180, command_timeout_s=20, seen_uuids=()):
    """由统一编排调用一次 CPU 或 GPU；成功/失败都保存 run.json 并返回字典。

    调用者负责镜像身份、开机稳定、屏幕与温度条件及采样器。本函数不重启、刷写、
    改电源或温控，只启动现有 Geekbench。GUI 的计算完成边界是稀疏观察区间，
    内部 runtime 原样保留，上传/持久化独立等待。GPU 默认 Vulkan，不能用整数
    API 枚举猜测接口。每次 out 必须是新目录。超时后保留应用现场，不杀掉未保存结果。
    """
    if kind not in ("cpu", "gpu") or mode not in (None, "native", "xhyper"):
        raise ValueError("kind must be cpu/gpu and mode native/xhyper/None")
    if api not in ("Vulkan", "OpenCL") or not 10 <= poll_interval_s <= 60:
        raise ValueError("invalid API or polling interval (10..60 seconds)")
    if not 1 <= compute_timeout_s <= 1800 or not 1 <= persist_timeout_s <= 600 or not 1 <= command_timeout_s <= 60:
        raise ValueError("invalid bounded timeout")
    target = Path(out)
    target.mkdir(parents=True, exist_ok=False)
    device = _Device(serial, target, command_timeout_s)
    result = {"valid": False, "run_id": target.name, "serial": serial, "mode": mode, "kind": kind,
              "requested_api": api if kind == "gpu" else None, "state": "prepared", "events": [],
              "measurement_validated": False, "observer": "sparse_uiautomator_and_sqlite_id",
              "poll_interval_s": poll_interval_s, "compute_complete_observed": False}

    def save_event(state, observation=None, **extra):
        result["state"] = state
        event = {"state": state, **extra}
        if observation:
            if observation["boot_id"] != result.get("boot_id", observation["boot_id"]):
                raise RunnerError("cross_boot_observation")
            event.update({key: observation[key] for key in ("boot_id", "before_s", "after_s")})
        result["events"].append(event)
        _write(target / "run.json", result)

    try:
        identity = device.identity()
        result["boot_id"] = identity["boot_id"]
        if expected_boot_id is not None and expected_boot_id != identity["boot_id"]:
            raise RunnerError("unexpected_boot_id")
        result["app"] = device.metadata()
        if result["app"]["version"] != expected_app_version:
            raise RunnerError("unexpected_app_version")
        # Baseline fallback is explicitly before this run, while no computation is active.
        if not device.sqlite_available:
            idle = device.observe("before-baseline")
            if idle["state"] in ("computing", "uploading"):
                raise RunnerError("another_geekbench_run_active")
        before, proof = device.snapshot("history-before", allow_stop=True)
        result["baseline_snapshot"] = proof
        baseline = max((rec["document_id"] for rec in before["records"]), default=0)
        click = device.start(kind, api)
        result["start_request_window"] = {key: click[key] for key in ("before_s", "after_s")}
        save_event("started", click)
        start_host = time.monotonic()
        persistence_started = None
        previous_computing = None
        latest_ui = None
        persisted = False

        def remaining_budget():
            if persistence_started is None:
                deadline = start_host + compute_timeout_s
                reason = "compute_or_unobserved_persistence_timeout"
            else:
                deadline = persistence_started + persist_timeout_s
                reason = "persistence_timeout_after_compute"
            device.deadline, device.deadline_reason = deadline, reason
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerError(reason)
            return remaining

        while True:
            remaining_budget()
            # Read a scalar ID first, then only its candidate JSON; never copy a live DB.
            query = device.new_document_id(baseline)
            remaining_budget()
            if query and query["boot_id"] != result["boot_id"]:
                raise RunnerError("cross_boot_observation")
            if query and query["new"]:
                persisted = True
                result["persisted_window"] = {key: query[key] for key in ("before_s", "after_s")}
                save_event("persisted", query)
                break
            try:
                observation = device.observe("poll-ui")
            except RunnerError as exc:
                remaining_budget()
                result["events"].append({"state": "ui_unavailable", "reason": str(exc)})
                observation = None
            remaining_budget()
            if observation:
                if observation["boot_id"] != result["boot_id"]:
                    raise RunnerError("cross_boot_observation")
                latest_ui = observation["payload"]
                state = observation["state"]
                if state == "benchmark_failed":
                    raise RunnerError("benchmark_failed_on_ui")
                if state == "computing":
                    previous_computing = observation
                    save_event("computing", observation)
                if state in ("uploading", "result", "upload_failed"):
                    if persistence_started is None:
                        persistence_started = time.monotonic()
                        result["compute_complete_observed"] = True
                        result["compute_complete_window"] = {
                            "lower_s": previous_computing["before_s"] if previous_computing else click["before_s"],
                            "upper_s": observation["after_s"],
                            "method": "last_computing_to_first_upload_or_result_observation"}
                        save_event("compute_complete", observation)
                    save_event("result_pending", observation, ui_state=state)
                    if state == "upload_failed":
                        raise RunnerError("upload_failed_result_not_saved")
                    if state == "result" and not device.sqlite_available:
                        # Only this app is stopped; the final DB must still prove a new complete result.
                        time.sleep(min(2, remaining_budget()))
                        remaining_budget()
                        persisted = True
                        result["persistence_candidate"] = "result_ui_after_compute; final_database_must_confirm"
                        break
            time.sleep(min(poll_interval_s, remaining_budget()))

        device.deadline = None
        after, proof = device.snapshot("history-after", allow_stop=persisted)
        result["final_snapshot"] = proof
        end = device.identity()
        bound = bind_result(before, after, kind=kind, expected_version=expected_app_version,
                            boot_id=result["boot_id"], end_boot_id=end["boot_id"], api=api,
                            result_ui=latest_ui, seen_uuids=seen_uuids)
        result.update(valid=True, result_uuid=bound["uuid"], document_id=bound["document_id"],
                      result=bound, end_boottime_s=end["after_s"],
                      gpu_hardware_verified=bound["gpu_hardware_verified"],
                      internal_runtime_s=bound["document"].get("runtime"))
        if not result["compute_complete_observed"]:
            result["compute_complete_window"] = None
            result["compute_boundary_note"] = "result persisted before a compute-end UI transition was observed; do not equate persistence time with compute end"
        _write(target / "result.json", bound["document"])
        save_event("validated", end)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result.update(valid=False, state="failed", reason=str(exc), error_type=type(exc).__name__)
        device.deadline = None
        if "timeout" in str(exc) and device.sqlite_available:
            try:
                _, result["failure_snapshot"] = device.snapshot("history-on-failure", allow_stop=False)
            except (ValueError, OSError, subprocess.SubprocessError) as snapshot_error:
                result["failure_snapshot_error"] = str(snapshot_error)
        device.failure_evidence()
        _write(target / "failure.json", {"valid": False, "reason": str(exc), "error_type": type(exc).__name__})
        _write(target / "run.json", result)
    return result
