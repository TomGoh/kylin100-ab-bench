"""Read-only supply observations and strict offline window binding.

Endpoint observations establish the reported source-state boundary. They do not
calibrate sensors or prove that an unobserved reconnect never occurred.
"""
import hashlib
import json
import math
import re
from uuid import UUID


READ_COMMAND = r"""printf 'ABSUPPLY\t1\n'
printf 'BEGIN\t%s\t%s\t%s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$(getprop ro.serialno)" "$(cut -d' ' -f1 /proc/uptime)"
for supply_path in /sys/class/power_supply/*; do
  [ -d "$supply_path" ] || continue
  supply_type=$(cat "$supply_path/type" 2>/dev/null) || supply_type='?'
  supply_online=$(cat "$supply_path/online" 2>/dev/null) || supply_online='?'
  supply_present=$(cat "$supply_path/present" 2>/dev/null) || supply_present='?'
  printf 'NODE\t%s\t%s\t%s\t%s\n' "$supply_path" "$supply_type" "$supply_online" "$supply_present"
done
printf 'DUMP_BEGIN\n'
dumpsys battery
printf 'DUMP_END\n'
printf 'END\t%s\t%s\t%s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$(getprop ro.serialno)" "$(cut -d' ' -f1 /proc/uptime)"
"""


def inventory_digest(nodes):
    inventory = sorted(({"path": node["path"], "type": node["type"]} for node in nodes), key=lambda node: node["path"])
    return hashlib.sha256(json.dumps(inventory, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _classify_sources(result, profile):
    """Evidence binds software roles, not proof of hardware isolation."""
    nodes = result["sysfs_sources"]
    result["supply_inventory_sha256"] = inventory_digest(nodes)
    exclusions = {}
    entries = profile.get("auxiliary_supply_paths") if isinstance(profile, dict) else None
    if entries:
        if (profile.get("serial") != result["physical_serial"]
                or profile.get("supply_inventory_sha256") != result["supply_inventory_sha256"]):
            return "auxiliary_supply_profile_identity_or_inventory_mismatch"
        if not isinstance(entries, list):
            return "invalid_auxiliary_supply_roles"
        by_path = {node["path"]: node for node in nodes}
        for entry in entries:
            if (not isinstance(entry, dict) or entry.get("role") not in ("battery_gauge", "charger_aux", "battery_limit")
                    or not isinstance(entry.get("evidence_reference"), str) or not entry["evidence_reference"].strip()
                    or not isinstance(entry.get("path"), str) or entry["path"] in exclusions):
                return "invalid_auxiliary_supply_roles"
            node = by_path.get(entry["path"])
            if node is None or entry.get("type") != node["type"] or node["type"] != "Unknown":
                return "auxiliary_supply_path_or_type_mismatch"
            if node["online_raw"] != "?":
                return "auxiliary_role_cannot_exclude_online_source"
            exclusions[entry["path"]] = entry
    for node in nodes:
        if node["path"] in exclusions:
            node.update(external_source=False, auxiliary_role=exclusions[node["path"]]["role"],
                        role_evidence_reference=exclusions[node["path"]]["evidence_reference"])
    external = [node for node in nodes if node["external_source"]]
    if not external or any(node["online"] is None for node in external):
        return "external_supply_state_unknown"
    if any(result["dumpsys_powered"].values()) or any(node["online"] is True for node in external):
        return "external_supply_online_or_contradictory"
    result["all_external_off"] = True
    return None


def parse_supply_output(raw, *, profile=None):
    """Preserve complete acquisition; unresolved input roles remain unverified."""
    result = {"valid": False, "all_external_off": False, "reason": None,
              "raw": raw, "sensor_calibrated": False,
              "method": "sysfs_enumeration_and_real_battery_service_endpoints"}
    def reject(reason):
        result["reason"] = reason
        return result
    if not isinstance(raw, str):
        return reject("missing_supply_raw")
    result["raw_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    lines = raw.splitlines()
    try:
        if not lines or lines[0] != "ABSUPPLY\t1":
            return reject("invalid_supply_protocol")
        begin, end = lines[1].split("\t"), lines[-1].split("\t")
        if len(begin) != 4 or len(end) != 4 or begin[0] != "BEGIN" or end[0] != "END":
            return reject("incomplete_supply_identity")
        UUID(begin[1])
        if begin[1:3] != end[1:3] or not begin[2].strip():
            return reject("supply_identity_changed_or_missing")
        times = float(begin[3]), float(end[3])
        if any(not math.isfinite(t) or t < 0 for t in times) or times[1] < times[0]:
            return reject("invalid_supply_time")
        result.update(boot_id=begin[1], physical_serial=begin[2],
                      t_start_s=times[0], t_end_s=times[1], clock="proc_uptime_boottime")
        dump_start, dump_end = lines.index("DUMP_BEGIN"), lines.index("DUMP_END")
        if dump_end <= dump_start or dump_end != len(lines) - 2:
            return reject("incomplete_battery_service_dump")
        nodes = []
        for line in lines[2:dump_start]:
            parts = line.split("\t")
            if len(parts) != 5 or parts[0] != "NODE" or not re.fullmatch(r"/sys/class/power_supply/[^/\s]+", parts[1]):
                return reject("incomplete_supply_enumeration")
            nodes.append({"path": parts[1], "type": parts[2], "online_raw": parts[3], "present_raw": parts[4]})
        result["sysfs_sources"] = nodes
        if not nodes or len({n["path"] for n in nodes}) != len(nodes):
            return reject("missing_or_duplicate_supply_nodes")
        batteries = [n for n in nodes if n["type"] == "Battery"]
        external = [n for n in nodes if n["type"] != "Battery"]
        if not batteries or any(n["present_raw"] != "1" for n in batteries) or not external:
            return reject("battery_or_external_enumeration_incomplete")
        if any(n["type"] in ("", "?") or n["online_raw"] not in ("0", "1", "?") for n in external):
            return reject("invalid_supply_type_or_online_value")
        for node in nodes:
            node["external_source"] = node["type"] != "Battery"
            node["online"] = (node["online_raw"] == "1") if node["online_raw"] in ("0", "1") else None
        dump = "\n".join(lines[dump_start + 1:dump_end])
        result["dumpsys_battery_raw"] = dump
        if re.search(r"updates\s+stopped", dump, re.I):
            return reject("battery_service_updates_stopped")
        powered = {}
        for name in ("AC", "USB", "Wireless", "Dock"):
            values = re.findall(r"^\s*" + name + r" powered:\s*(true|false)\s*$", dump, re.M)
            if len(values) != 1:
                return reject("missing_or_duplicate_real_powered_flags")
            powered[name] = values[0] == "true"
        result["dumpsys_powered"] = powered
        result["valid"] = True
        result["reason"] = _classify_sources(result, profile)
        return result
    except (ValueError, IndexError, TypeError):
        return reject("invalid_or_incomplete_supply_output")


def read_supply(serial):
    """Read only; caller stores the returned raw evidence with its capture."""
    from .capture import shell
    result = parse_supply_output(shell(serial, READ_COMMAND, timeout=12).stdout)
    result["transport_serial"] = serial
    return result


def verify_supply_snapshot(snapshot, *, physical_serial, profile=None):
    """Check a single observation, without inventing a covered duration."""
    parsed = parse_supply_output(snapshot.get("raw") if isinstance(snapshot, dict) else None, profile=profile)
    parsed["verified_off"] = False
    if not isinstance(physical_serial, str) or not physical_serial.strip():
        parsed["reason"] = "missing_physical_supply_identity"
    elif parsed["valid"] and parsed["physical_serial"] != physical_serial:
        parsed["reason"] = "supply_physical_identity_mismatch"
    elif parsed["valid"] and parsed["all_external_off"]:
        parsed["verified_off"] = True
    return parsed


def verify_supply_window(before, after, *, boot_id, physical_serial, start_s, end_s, profile=None):
    """Reparse raw evidence instead of trusting caller-set isolation flags."""
    result = {"verified_off": False, "reason": None, "sensor_calibrated": False,
              "method": "all_reported_sources_off_at_same_boot_bracketing_endpoints",
              "continuity_note": "software_roles_not_hardware_isolation_proof_endpoint_observations_only_cable_must_physically_remain_unplugged_no_other_input"}
    def reject(reason):
        result["reason"] = reason
        return result
    if not isinstance(physical_serial, str) or not physical_serial.strip():
        return reject("missing_physical_supply_identity")
    try:
        valid_bounds = (type(start_s) in (int, float) and type(end_s) in (int, float)
                        and math.isfinite(start_s) and math.isfinite(end_s)
                        and start_s >= 0 and end_s > start_s)
    except OverflowError:
        valid_bounds = False
    if not valid_bounds:
        return reject("invalid_supply_window")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return reject("missing_bracketing_supply_evidence")
    observations = [parse_supply_output(item.get("raw"), profile=profile) for item in (before, after)]
    result["observations"] = observations
    if any(not item["valid"] for item in observations):
        return reject(next(item["reason"] for item in observations if not item["valid"]))
    if any(item["boot_id"] != boot_id or item["physical_serial"] != physical_serial for item in observations):
        return reject("supply_boot_or_physical_identity_mismatch")
    if observations[0]["t_end_s"] > start_s or observations[1]["t_start_s"] < end_s:
        return reject("supply_evidence_does_not_bracket_window")
    inventories = [{(n["path"], n["type"]) for n in item["sysfs_sources"]} for item in observations]
    if inventories[0] != inventories[1]:
        return reject("supply_inventory_changed")
    if any(not item["all_external_off"] for item in observations):
        return reject(next(item["reason"] for item in observations if not item["all_external_off"]))
    result.update(verified_off=True, source_before_sha256=observations[0]["raw_sha256"],
                  source_after_sha256=observations[1]["raw_sha256"],
                  window_start_s=start_s, window_end_s=end_s)
    return result
