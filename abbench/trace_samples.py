"""Strictly bind exported Perfetto battery counters to one device boot.

Conversion validity only describes schema, units, timestamp binding and complete
batches. It does not calibrate the battery gauge or establish device power.
"""

import csv
import hashlib
import io
import math
import re
from collections.abc import Mapping
from pathlib import Path


_COLUMNS = (
    "ts_ns", "track_id", "name", "type", "unit", "source_arg_set_id",
    "dimension_arg_set_id", "value",
)
_FIELDS = {
    "batt.current_ua": "current_ua",
    "batt.voltage_uv": "voltage_uv",
    "batt.charge_uah": "charge_uah",
}
_UNITS = {"batt.current_ua": "uA", "batt.voltage_uv": "uV", "batt.charge_uah": "uAh"}
_NULLS = {"", "[NULL]"}


def _failure(reason, identity=None, **details):
    return {
        "valid": False, "reason": reason, "samples": [],
        "measurement_validated": False, "counter_validated": False,
        "input_supply_verified_off": False, "source_csv": identity,
        **details,
    }


def _integer(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError("not a nonnegative integer")
    parsed = int(value)
    if parsed > 2 ** 63 - 1:
        raise ValueError("integer exceeds the exported signed 64-bit domain")
    return parsed


def _arg_id(value):
    return None if value in _NULLS else _integer(value)


def _binding_reason(evidence, boot_id, digest):
    if not isinstance(evidence, Mapping):
        return "missing_source_evidence"
    if evidence.get("boot_id") != boot_id:
        return "source_boot_binding_mismatch"
    if evidence.get("csv_sha256") != digest:
        return "source_csv_binding_mismatch"
    if not isinstance(evidence.get("reference"), str) or not evidence["reference"].strip():
        return "missing_evidence_reference"
    return None


def normalize_battery_csv(csv_path, *, boot_id, source_verified=False,
                          source_evidence=None, external_online=None,
                          supply_evidence=None):
    """Return power.integrate-compatible samples without nearest matching.

    source_verified=True means the caller checked units and the timestamp-to-boot
    binding. source_evidence must bind this CSV's SHA-256 and boot_id and declare
    clock='CLOCK_BOOTTIME', ts_unit='ns', units matching _UNITS, and a nonempty
    reference to the verification record. No clock offset or unit conversion is
    guessed. A supplied CSV boot_id column must agree with that same binding.

    external_online defaults to unknown. To supply a boolean, supply_evidence
    must bind the same CSV/boot, declare external_online with the same boolean,
    state_verified=True and covers_entire_window=True, and provide a reference.
    Cable presence or a successful stop-charging command is not that evidence.

    A missing charge track is allowed and yields charge_uah=None; when present,
    it must have exactly the same timestamp set as current and voltage. One
    complete batch is structurally valid; integration still requires two.
    """
    if not isinstance(boot_id, str) or not boot_id.strip():
        return _failure("invalid_boot_id")
    if source_verified is not True:
        return _failure("source_units_and_clock_not_verified")
    if external_online is not None and not isinstance(external_online, bool):
        return _failure("invalid_external_online")
    try:
        path = Path(csv_path).expanduser().resolve()
        raw = path.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        return _failure("source_csv_read_failed", error=str(exc))
    identity = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
    reason = _binding_reason(source_evidence, boot_id, identity["sha256"])
    if reason:
        return _failure(reason, identity)
    if source_evidence.get("clock") != "CLOCK_BOOTTIME" or source_evidence.get("ts_unit") != "ns":
        return _failure("unsupported_or_unverified_clock", identity)
    if source_evidence.get("units") != _UNITS:
        return _failure("unconfirmed_battery_units", identity)
    if external_online is not None:
        reason = _binding_reason(supply_evidence, boot_id, identity["sha256"])
        if reason:
            return _failure("supply_" + reason, identity)
        if (supply_evidence.get("state_verified") is not True
                or supply_evidence.get("covers_entire_window") is not True
                or supply_evidence.get("external_online") is not external_online):
            return _failure("external_supply_state_not_verified_for_window", identity)
    elif supply_evidence is not None:
        return _failure("supply_evidence_without_explicit_state", identity)

    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
        headers = reader.fieldnames
        if not headers or len(headers) != len(set(headers)) or not set(_COLUMNS).issubset(headers):
            return _failure("invalid_csv_schema", identity)
        tracks = {}
        battery_ids = {}
        values = {name: {} for name in _FIELDS}
        seen_points = set()
        previous_ts = None
        row_count = 0
        for row in reader:
            row_count += 1
            line = reader.line_num
            if None in row or any(row.get(key) is None for key in _COLUMNS):
                return _failure("invalid_csv_row", identity, source_line=line)
            if "boot_id" in row and row["boot_id"] != boot_id:
                return _failure("row_boot_binding_mismatch", identity, source_line=line)
            try:
                ts_ns = _integer(row["ts_ns"])
                track_id = _integer(row["track_id"])
                source_arg = _arg_id(row["source_arg_set_id"])
                dimension_arg = _arg_id(row["dimension_arg_set_id"])
                value = float(row["value"])
            except (ValueError, OverflowError):
                return _failure("invalid_counter_number", identity, source_line=line)
            if not math.isfinite(value):
                return _failure("nonfinite_counter_value", identity, source_line=line)
            if previous_ts is not None and ts_ns < previous_ts:
                return _failure("counter_timestamps_out_of_order", identity, source_line=line)
            previous_ts = ts_ns
            if (track_id, ts_ns) in seen_points:
                return _failure("duplicate_counter_point", identity, source_line=line)
            seen_points.add((track_id, ts_ns))
            if not row["name"] or not row["type"]:
                return _failure("missing_track_identity", identity, source_line=line)
            metadata = {
                "name": row["name"], "type": row["type"], "raw_unit": row["unit"],
                "source_arg_set_id": source_arg, "dimension_arg_set_id": dimension_arg,
            }
            if track_id in tracks and tracks[track_id] != metadata:
                return _failure("track_id_identity_conflict", identity, source_line=line)
            tracks[track_id] = metadata
            name = row["name"]
            if name not in _FIELDS:
                continue
            if row["type"] != "battery_counter":
                return _failure("unexpected_battery_track_type", identity, source_line=line)
            if row["unit"] not in _NULLS and row["unit"] != _UNITS[name]:
                return _failure("csv_unit_conflicts_with_verified_binding", identity, source_line=line)
            if name in battery_ids and battery_ids[name] != track_id:
                return _failure("multiple_tracks_for_battery_field", identity, source_line=line)
            battery_ids[name] = track_id
            if name == "batt.voltage_uv" and value <= 0:
                return _failure("invalid_voltage_uv", identity, source_line=line)
            if name == "batt.charge_uah" and value < 0:
                return _failure("invalid_charge_uah", identity, source_line=line)
            values[name][ts_ns] = {"value": value, "source_line": line}
    except (UnicodeError, csv.Error) as exc:
        return _failure("invalid_csv_encoding_or_syntax", identity, error=str(exc))

    required_names = ("batt.current_ua", "batt.voltage_uv")
    missing = [name for name in required_names if not values[name]]
    if missing:
        return _failure("missing_required_battery_track", identity, missing_tracks=missing)
    times = list(values["batt.current_ua"])
    for name in battery_ids:
        if set(values[name]) != set(times):
            return _failure("unsynchronized_battery_timestamps", identity, track_name=name)
    samples = []
    previous_time = None
    for ts_ns in times:
        time_s = ts_ns / 1e9
        if previous_time is not None and time_s <= previous_time:
            return _failure("timestamp_precision_loss", identity)
        sample = {
            "t_s": time_s, "raw_ts_ns": ts_ns, "boot_id": boot_id,
            "external_online": external_online, "charge_uah": None,
            "source_csv_sha256": identity["sha256"],
            "source_track_ids": { _FIELDS[name]: track_id for name, track_id in battery_ids.items() },
            "declared_units": { _FIELDS[name]: _UNITS[name] for name in battery_ids },
            "source_row_numbers": {},
        }
        for name in battery_ids:
            field = _FIELDS[name]
            sample[field] = values[name][ts_ns]["value"]
            sample["source_row_numbers"][field] = values[name][ts_ns]["source_line"]
        samples.append(sample)
        previous_time = time_s
    return {
        "valid": True, "reason": None, "samples": samples, "source_csv": identity,
        "boot_id": boot_id, "clock": "CLOCK_BOOTTIME", "source_verified": True,
        "source_evidence": dict(source_evidence),
        "supply_evidence": dict(supply_evidence) if supply_evidence is not None else None,
        "external_online": external_online, "raw_row_count": row_count,
        "raw_track_count": len(tracks), "selected_track_count": len(battery_ids),
        "sample_count": len(samples), "selected_track_ids": dict(battery_ids),
        "tracks": {str(track_id): metadata for track_id, metadata in tracks.items()},
        "measurement_validated": False, "counter_validated": False,
        "input_supply_verified_off": False,
        "validity_scope": "conversion_only_not_sensor_calibration_or_device_power",
        "timestamp_note": "shared_export_timestamp_not_proof_of_simultaneous_hardware_read",
    }
