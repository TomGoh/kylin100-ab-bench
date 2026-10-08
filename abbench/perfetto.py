"""Use the official Trace Processor CLI; never decode trace protobuf ourselves."""
import csv
import io
import json
import math
import re
import subprocess
from pathlib import Path


def _counter_table(output, name):
    reader = csv.DictReader(io.StringIO(output))
    common = {"track_id", "name", "type", "unit", "source_arg_set_id", "dimension_arg_set_id"}
    extra = ({"sample_count", "first_ts_ns", "last_ts_ns", "min_value", "max_value"}
             if name == "counter-inventory" else {"ts_ns", "value"})
    if not reader.fieldnames or not common.union(extra).issubset(reader.fieldnames):
        raise ValueError("unexpected Trace Processor output schema")
    rows = list(reader)
    if not rows:
        raise ValueError("trace has no counter telemetry; do not substitute zero")
    identities = set()
    for row in rows:
        if None in row or any(row.get(field) is None for field in common.union(extra)):
            raise ValueError("malformed Trace Processor counter row")
        if not row["name"].strip() or not row["type"].strip() or int(row["track_id"]) < 0:
            raise ValueError("invalid counter identity")
        if name == "counter-inventory":
            first, last = int(row["first_ts_ns"]), int(row["last_ts_ns"])
            if int(row["sample_count"]) <= 0 or first < 0 or last < first:
                raise ValueError("counter inventory has no valid sample coverage")
            values = [float(row["min_value"]), float(row["max_value"])]
            identity = int(row["track_id"])
        else:
            timestamp = int(row["ts_ns"])
            if timestamp < 0:
                raise ValueError("invalid counter timestamp")
            values = [float(row["value"])]
            identity = (int(row["track_id"]), timestamp)
        if identity in identities or not all(math.isfinite(value) for value in values):
            raise ValueError("duplicate or nonfinite counter telemetry")
        identities.add(identity)
    return rows


def assess_powerstats(process_text, service_dump):
    """A registered HAL or visible rail is insufficient evidence of a real meter."""
    example = bool(re.search(r"\bandroid\.hardware\.power\.stats-service\.example\b", process_text))
    return {
        "example_service_detected": example,
        "channels_advertised": "ChannelId:" in service_dump,
        "rail_measurement_validated": False,
        "energy_consumer_measurement_validated": False,
        "state_residency_validated": False,
        "reason": "example_service_fake_data" if example else "hardware_source_not_yet_validated",
        "battery_health_source_requires_separate_validation": True,
    }


def export_trace(trace_path, processor_path, directory, timeout_s=30):
    """Export raw counters through pinned official tooling, without power claims.

    The supplied processor must already be installed. No auto-download, SQL
    generation from user input, source calibration or result-to-run binding is
    performed. Keep the raw trace with the exported tables.
    """
    trace = Path(trace_path).expanduser().resolve()
    processor = Path(processor_path).expanduser().resolve()
    if not trace.is_file() or not processor.is_file():
        raise ValueError("trace and installed Trace Processor files are required")
    if not 0 < timeout_s <= 60:
        raise ValueError("timeout must be >0 and <=60 seconds")
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=False)
    result = {"source_trace": str(trace), "processor": str(processor),
              "measurement_validated": False, "tables": {}}
    try:
        version = subprocess.run([str(processor), "--version"], capture_output=True,
                                 text=True, timeout=timeout_s, check=True)
        result["processor_version"] = version.stdout.strip()
        sql_dir = Path(__file__).resolve().parents[1] / "sql"
        for name in ("counter-inventory", "counter-samples"):
            proc = subprocess.run([str(processor), str(trace), "-q", str(sql_dir / (name + ".sql"))],
                                  capture_output=True, text=True, timeout=timeout_s, check=True)
            (out / (name + ".csv")).write_text(proc.stdout, encoding="utf-8")
            (out / (name + ".stderr.txt")).write_text(proc.stderr, encoding="utf-8")
            rows = _counter_table(proc.stdout, name)
            result["tables"][name] = {"rows": len(rows), "file": name + ".csv"}
        (out / "export.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        failure = {"valid": False, "error": type(exc).__name__}
        for field in ("stdout", "stderr"):
            value = getattr(exc, field, None) or ""
            if isinstance(value, bytes):
                value = value.decode(errors="replace")
            (out / ("failure." + field + ".txt")).write_text(value, encoding="utf-8")
        (out / "failure.json").write_text(json.dumps(failure) + "\n", encoding="utf-8")
        raise ValueError("official Trace Processor failed or timed out") from exc
    except ValueError as exc:
        (out / "failure.json").write_text(json.dumps({"valid": False, "error": str(exc)}) + "\n", encoding="utf-8")
        raise
