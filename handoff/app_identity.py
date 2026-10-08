#!/usr/bin/env python3
"""Print the Geekbench 6 identity the formal manifest needs (app_version, app_apk_sha256), computed exactly like
abbench/geekbench_runner.py: sha256 of every installed APK, sorted by file name, then sha256 of that JSON list.

usage: python3 handoff/app_identity.py --serial T11206HXH6600007 [--manifest handoff/formal-manifest-cmp1.json]
With --manifest it also writes both values into images.native and images.xhyper (the two sides must be equal).
"""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

PACKAGE = "com.primatelabs.geekbench6"


def shell(serial, command):
    p = subprocess.run(["adb", "-s", serial, "shell", "su 0 sh -c '%s'" % command],
                       capture_output=True, text=True, timeout=60)
    return p.stdout.replace("\r", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    ap.add_argument("--manifest")
    args = ap.parse_args()
    version = re.search(r"versionName=(\S+)", shell(args.serial, "dumpsys package " + PACKAGE))
    paths = [line[len("package:"):] for line in shell(args.serial, "pm path " + PACKAGE).splitlines()
             if line.startswith("package:")]
    if not version or not paths:
        raise SystemExit("Geekbench 6 is not installed on the tablet (install it and accept its first-run notice first)")
    files = []
    for line in shell(args.serial, "sha256sum " + " ".join(paths)).splitlines():
        m = re.fullmatch(r"([0-9a-f]{64})\s+\*?(.+)", line.strip())
        if m:
            files.append({"name": Path(m.group(2)).name, "sha256": m.group(1)})
    if len(files) != len(paths):
        raise SystemExit("could not hash every installed APK: %s" % paths)
    files.sort(key=lambda item: item["name"])
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    print(json.dumps({"app_version": version.group(1), "app_apk_sha256": digest, "apk_files": files}, indent=2))
    if args.manifest:
        data = json.loads(Path(args.manifest).read_text())
        for mode in ("native", "xhyper"):
            data["images"][mode]["app_version"] = version.group(1)
            data["images"][mode]["app_apk_sha256"] = digest
        Path(args.manifest).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("written into", args.manifest)


if __name__ == "__main__":
    main()
