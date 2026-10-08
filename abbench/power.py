"""Offline battery energy calculations with explicit measurement boundaries.

All voltages are microvolts, currents microamps and charge counters microamp-hours.
Times must be device CLOCK_BOOTTIME seconds from one boot.  Positive returned
energy means net battery discharge; negative energy means net battery charging.
Neither a battery net reading nor a charging toggle proves whole-device power.

external_online describes the verified actual external power state. A raw USB
online value may instead describe cable presence; passing it through unchanged
is conservative and can refuse a whole-device boundary despite a software input
suspend request. Never automatically translate that raw flag into verified input
isolation.
"""

from collections.abc import Mapping
from math import fsum, isfinite
from numbers import Real


_BOUNDARIES = {"battery_net", "battery_side_device"}


def _number(value):
    if not isinstance(value, Real) or isinstance(value, bool):
        return False
    try:
        return isfinite(value)
    except (OverflowError, ValueError):
        return False


def _result(config, method, reason=None, **fields):
    boundary = config.get("power_boundary") if isinstance(config, Mapping) else None
    result = {
        "valid": reason is None,
        "reason": reason,
        "invalid_reason": reason,
        "duration_s": None,
        "energy_j": None,
        "energy_mwh": None,
        "mean_w": None,
        "power_boundary": boundary,
        "method": method,
        "energy_sign": "positive_discharge_negative_charge",
    }
    result.update(fields)
    return result


def _config_reason(config, *, needs_current):
    if not isinstance(config, Mapping):
        return "invalid_config"
    if not isinstance(config.get("power_boundary"), str) or config["power_boundary"] not in _BOUNDARIES:
        return "invalid_power_boundary"
    verified = config.get("input_supply_verified_off", False)
    if not isinstance(verified, bool):
        return "invalid_supply_verification"
    if config["power_boundary"] == "battery_side_device" and not verified:
        return "input_supply_not_verified_off"
    if needs_current:
        sign = config.get("discharge_sign")
        if not _number(sign) or sign not in (-1, 1):
            return "invalid_discharge_sign"
        gap = config.get("max_gap_s")
        if not _number(gap) or gap <= 0:
            return "invalid_max_gap_s"
    return None


def _sample_reason(sample, *, needs_current, needs_charge, boundary):
    if not isinstance(sample, Mapping):
        return "invalid_sample"
    required = ["t_s", "boot_id", "voltage_uv", "external_online"]
    if needs_current:
        required.append("current_ua")
    if needs_charge:
        required.append("charge_uah")
    if any(key not in sample for key in required):
        return "missing_sample_field"
    if not isinstance(sample["boot_id"], str) or not sample["boot_id"].strip():
        return "invalid_boot_id"
    if not _number(sample["t_s"]) or sample["t_s"] < 0:
        return "invalid_boottime"
    if not _number(sample["voltage_uv"]) or sample["voltage_uv"] <= 0:
        return "invalid_voltage_uv"
    if needs_current and not _number(sample["current_ua"]):
        return "invalid_current_ua"
    charge = sample.get("charge_uah")
    if needs_charge or charge is not None:
        if not _number(charge) or charge < 0:
            return "invalid_charge_uah"
    online = sample["external_online"]
    if online is not None and not isinstance(online, bool):
        return "invalid_external_online"
    if boundary == "battery_side_device":
        if online is None:
            return "external_supply_unknown"
        if online:
            return "external_supply_online"
    return None


def _linear_power(points, time_s):
    for index, (t_s, power_w) in enumerate(points):
        if time_s == t_s:
            return power_w
        if time_s < t_s:
            left_t, left_p = points[index - 1]
            fraction = (time_s - left_t) / (t_s - left_t)
            return left_p * (1 - fraction) + power_w * fraction
    return points[-1][1]


def integrate(samples, config):
    """Integrate signed power, rejecting bad records rather than sorting/filling.

    Required config: discharge_sign (+1/-1), power_boundary and max_gap_s.
    battery_side_device also requires input_supply_verified_off=True and explicit
    external_online=False on every sample.  That flag must represent verified
    input isolation for the complete run, not just a successful control command.

    Optional window_start_s/window_end_s (None means unspecified) must lie within
    observed coverage. Power
    is piecewise linear between original samples; clipped boundaries interpolate
    POWER, not separate current and voltage signals. Gaps are checked across the
    complete input, including samples outside the selected window.
    """
    method = "sampled_power_trapezoid"
    reason = _config_reason(config, needs_current=True)
    if reason:
        return _result(config, method, reason)
    try:
        rows = list(samples)
    except TypeError:
        return _result(config, method, "invalid_samples")
    if len(rows) < 2:
        return _result(config, method, "insufficient_samples")

    points = []
    boot_id = None
    previous_time = None
    for index, sample in enumerate(rows):
        reason = _sample_reason(
            sample, needs_current=True, needs_charge=False,
            boundary=config["power_boundary"],
        )
        if reason:
            return _result(config, method, reason, invalid_sample_index=index)
        if boot_id is None:
            boot_id = sample["boot_id"]
        elif sample["boot_id"] != boot_id:
            return _result(config, method, "cross_boot_samples", invalid_sample_index=index)
        t_s = sample["t_s"]
        if previous_time is not None:
            if t_s <= previous_time:
                return _result(config, method, "non_increasing_time", invalid_sample_index=index)
            if t_s - previous_time > config["max_gap_s"]:
                return _result(config, method, "sample_gap_exceeded", invalid_sample_index=index)
        power_w = (sample["voltage_uv"] / 1e6) * (sample["current_ua"] / 1e6) * config["discharge_sign"]
        if not isfinite(power_w):
            return _result(config, method, "nonfinite_calculated_power", invalid_sample_index=index)
        if config["power_boundary"] == "battery_side_device" and power_w < 0:
            return _result(config, method, "charging_in_verified_battery_window", invalid_sample_index=index)
        points.append((t_s, power_w))
        previous_time = t_s

    start = config.get("window_start_s")
    end = config.get("window_end_s")
    start = points[0][0] if start is None else start
    end = points[-1][0] if end is None else end
    if not _number(start) or not _number(end) or end <= start:
        return _result(config, method, "invalid_window")
    if start < points[0][0] or end > points[-1][0]:
        return _result(config, method, "window_outside_sample_coverage")
    clipped = [(start, _linear_power(points, start))]
    clipped.extend(point for point in points if start < point[0] < end)
    clipped.append((end, _linear_power(points, end)))
    try:
        energy_j = fsum(
            (left[1] / 2 + right[1] / 2) * (right[0] - left[0])
            for left, right in zip(clipped, clipped[1:])
        )
    except (OverflowError, ValueError):
        return _result(config, method, "nonfinite_calculated_energy")
    duration = end - start
    if not isfinite(energy_j) or not isfinite(duration):
        return _result(config, method, "nonfinite_calculated_energy")
    mean_w = energy_j / duration
    if not isfinite(mean_w):
        return _result(config, method, "nonfinite_calculated_power")
    observed_powers = [power for time, power in points if start <= time <= end]
    boundary_interpolated = start not in [p[0] for p in points] or end not in [p[0] for p in points]
    return _result(
        config, method, boot_id=boot_id, duration_s=duration,
        energy_j=energy_j, energy_mwh=energy_j / 3.6, mean_w=mean_w,
        window_start_s=start, window_end_s=end, sample_count=len(rows),
        window_sample_count=len(observed_powers),
        window_clipped=start != points[0][0] or end != points[-1][0],
        boundary_interpolated=boundary_interpolated,
        interpolation="linear_power" if boundary_interpolated else None,
        max_sampled_power_w=max(observed_powers) if observed_powers else None,
        maximum_note="sample_maximum_not_true_instantaneous_peak",
        discharge_sign=config["discharge_sign"],
        input_supply_verified_off=config.get("input_supply_verified_off", False),
    )


def endpoint(start, end, config):
    """Estimate signed energy from validated remaining-charge endpoints.

    The real endpoint boottimes determine duration, including system suspend.
    Energy uses arithmetic mean endpoint voltage, an explicitly labelled
    approximation.  counter_resolution_uah is optional; when unknown, no
    resolution claim is possible. Zero delta always fails rather than implying
    zero power. The caller must verify stable input isolation for the entire
    window if selecting battery_side_device; endpoints alone cannot prove it.
    """
    method = "charge_endpoints_mean_voltage_approximation"
    reason = _config_reason(config, needs_current=False)
    if reason:
        return _result(config, method, reason)
    if config.get("counter_validated") is not True:
        return _result(config, method, "counter_not_validated")
    resolution = config.get("counter_resolution_uah")
    if resolution is not None and (not _number(resolution) or resolution <= 0):
        return _result(config, method, "invalid_counter_resolution_uah")
    for label, sample in (("start", start), ("end", end)):
        reason = _sample_reason(
            sample, needs_current=False, needs_charge=True,
            boundary=config["power_boundary"],
        )
        if reason:
            return _result(config, method, reason, invalid_endpoint=label)
    if start["boot_id"] != end["boot_id"]:
        return _result(config, method, "cross_boot_samples")
    duration = end["t_s"] - start["t_s"]
    if duration <= 0:
        return _result(config, method, "non_increasing_time")
    if any(config.get(key) is not None and not _number(config[key])
           for key in ("window_start_s", "window_end_s")):
        return _result(config, method, "invalid_window")
    # The original endpoint measurement cannot be retimed by interpolating charge.
    if (config.get("window_start_s") is not None and config["window_start_s"] != start["t_s"]) or (
        config.get("window_end_s") is not None and config["window_end_s"] != end["t_s"]
    ):
        return _result(config, method, "endpoint_window_mismatch")
    delta_q = start["charge_uah"] - end["charge_uah"]
    if delta_q == 0 or (resolution is not None and abs(delta_q) < resolution):
        return _result(config, method, "below_counter_resolution", duration_s=duration,
                       delta_charge_uah=delta_q, counter_resolution_uah=resolution)
    if config["power_boundary"] == "battery_side_device" and delta_q < 0:
        return _result(config, method, "charging_in_verified_battery_window")
    mean_voltage_v = start["voltage_uv"] / 2e6 + end["voltage_uv"] / 2e6
    energy_mwh = delta_q / 1000 * mean_voltage_v
    energy_j = energy_mwh * 3.6
    if not isfinite(duration) or not isfinite(energy_j):
        return _result(config, method, "nonfinite_calculated_energy")
    mean_w = energy_j / duration
    mean_current_ma = delta_q / 1000 * 3600 / duration
    if not isfinite(mean_w) or not isfinite(mean_current_ma):
        return _result(config, method, "nonfinite_calculated_power")
    return _result(
        config, method, boot_id=start["boot_id"], duration_s=duration,
        energy_j=energy_j, energy_mwh=energy_mwh, mean_w=mean_w,
        window_start_s=start["t_s"], window_end_s=end["t_s"],
        delta_charge_uah=delta_q, mean_current_ma=mean_current_ma,
        mean_voltage_v=mean_voltage_v, voltage_method="mean_of_two_endpoints",
        approximate=True, counter_resolution_uah=resolution,
        quantization_relative_bound=(resolution / abs(delta_q) * 2) if resolution is not None else None,
        resolution_known=resolution is not None,
        input_supply_verified_off=config.get("input_supply_verified_off", False),
    )
