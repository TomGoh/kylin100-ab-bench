"""Validate the image handoff contract without pretending to verify a device."""
import re


def validate_manifest(data):
    issues = []
    if not isinstance(data, dict):
        return {"valid": False, "issues": ["manifest_must_be_object"]}
    images = data.get("images", {})
    if not isinstance(images, dict):
        return {"valid": False, "issues": ["images_must_be_object"]}
    for mode in ("native", "xhyper"):
        image = images.get(mode)
        if not isinstance(image, dict):
            issues.append(f"{mode}:missing_image")
            continue
        for field in ("image_sha256", "host_kernel_sha256", "app_apk_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(image.get(field, ""))):
                issues.append(f"{mode}:invalid_{field}")
        for field in ("android_fingerprint", "kernel_runtime_id", "app_version", "mode_evidence"):
            if not isinstance(image.get(field), str) or not image[field].strip():
                issues.append(f"{mode}:missing_{field}")
        if image.get("normal_configuration_confirmed") is not True:
            issues.append(f"{mode}:normal_configuration_unconfirmed")
        if image.get("identity_verified_on_device") is not True:
            issues.append(f"{mode}:device_identity_unconfirmed")
    if all(isinstance(images.get(mode), dict) for mode in ("native", "xhyper")):
        for field in ("host_kernel_sha256", "android_fingerprint", "kernel_runtime_id", "app_apk_sha256", "app_version"):
            if images["native"].get(field) != images["xhyper"].get(field):
                issues.append(f"pair:mismatched_{field}")
        if images["native"].get("image_sha256") == images["xhyper"].get("image_sha256"):
            issues.append("pair:identical_mode_images_require_explanation")
    xhyper = images.get("xhyper")
    if isinstance(xhyper, dict):
        for field in ("xhyper_commit", "manager_commit", "build_configuration"):
            if not isinstance(xhyper.get(field), str) or not xhyper[field].strip():
                issues.append(f"xhyper:missing_{field}")
        if not isinstance(xhyper.get("diagnostic_features"), list):
            issues.append("xhyper:diagnostic_features_undeclared")
        elif xhyper["diagnostic_features"]:
            issues.append("xhyper:diagnostic_configuration_requires_separate_comparison")
    return {"valid": not issues, "issues": issues, "scope": "handoff_contract_only"}
