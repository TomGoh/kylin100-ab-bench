#!/usr/bin/env python3
"""Mode-switch adapter for kylin100-ab-bench on the T11206 tablet (image owner side).

usage: t11206_mode_switch.py --mode native|xhyper --serial SERIAL --output OUT [--images FILE]

Writes boot_a from an image copy kept on the tablet (pushing it first from a local file if the copy is missing or
wrong), reboots, waits for a NEW boot, and checks the identity: boot_a hash, slot _a, and the command-line mode token
(xhyper.mode=host present for xhyper, absent for native). It never writes boot_b (kept stock as the bootloader's
fallback) or vendor_boot (shared by both modes). It always reboots, also when boot_a already holds the target, because
the framework requires a new boot id. It does not create OUT; its log and result go to OUT + ".switch-<mode>.log/.json".
Exit 0 only when every check passed. Stdlib only; runs on macOS and Linux with adb in PATH. Use it over USB: with a
wireless address it tries `adb connect` after the reboot, which works only if adbd listens on that port again.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PARTITION = "/dev/block/by-name/boot_a"
XHYPER_TOKEN = "xhyper.mode=host"


class SwitchError(Exception):
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=("native", "xhyper"))
    ap.add_argument("--serial", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--images", default=os.path.join(HERE, "t11206-images.json"))
    ap.add_argument("--boot-timeout", type=int, default=240)
    args = ap.parse_args()
    base = args.output.rstrip("/") + ".switch-" + args.mode
    os.makedirs(os.path.dirname(os.path.abspath(base)), exist_ok=True)
    log = open(base + ".log", "a")
    result = {"mode": args.mode, "serial": args.serial, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    def say(msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    def adb(*cmd, timeout=30, check=True):
        full = ["adb", "-s", args.serial] + list(cmd)
        try:
            p = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SwitchError("adb timed out after %ss: %s" % (timeout, " ".join(cmd)))
        log.write("$ %s\n%s%s" % (" ".join(full), p.stdout, p.stderr))
        if check and p.returncode != 0:
            raise SwitchError("adb failed (rc=%d): %s: %s" % (p.returncode, " ".join(cmd), p.stderr.strip()))
        return p.stdout.replace("\r", "")

    def root(command, timeout=60, check=True):
        return adb("shell", "su 0 sh -c '%s'" % command, timeout=timeout, check=check)

    def sha(path, timeout=60):
        # A missing file is not an error here: the caller compares the result with the wanted hash.
        out = root("sha256sum %s 2>/dev/null" % path, timeout=timeout, check=False).split()
        return out[0] if out and len(out[0]) == 64 else ""

    def boot_id():
        return adb("shell", "cat /proc/sys/kernel/random/boot_id").strip()

    try:
        images = json.load(open(args.images))
        target = images[args.mode]
        want = target["sha256"]
        copy = target["device_copy"]
        result.update(target_sha256=want, device_copy=copy)
        if adb("get-state", timeout=20).strip() != "device":
            raise SwitchError("device not in adb 'device' state")
        old = boot_id()
        result["old_boot_id"] = old
        say("mode %s: target boot_a %s, old boot id %s" % (args.mode, want[:16], old))

        if sha(copy) != want:
            local = target.get("local_file")
            local = local if not local or os.path.isabs(local) else os.path.join(HERE, local)
            if not local or not os.path.isfile(local):
                raise SwitchError("tablet copy %s is missing or differs and no local file to push (%s)" % (copy, local))
            say("tablet copy missing or different: pushing %s" % local)
            adb("push", local, copy, timeout=300)
            if sha(copy) != want:
                raise SwitchError("pushed copy %s does not hash to %s" % (copy, want))

        for attempt in (1, 2):
            root("dd if=%s of=%s bs=4M conv=fsync" % (copy, PARTITION), timeout=180)
            got = sha(PARTITION, timeout=120)
            say("boot_a read back %s (attempt %d)" % (got[:16], attempt))
            if got == want:
                break
        else:
            raise SwitchError("boot_a reads back %s, not %s; NOT rebooting (boot_b is still stock)" % (got, want))

        say("rebooting")
        adb("reboot", timeout=30, check=False)
        deadline = time.time() + args.boot_timeout
        time.sleep(10)
        while True:
            if time.time() > deadline:
                raise SwitchError("device did not finish booting within %ss; if it never comes back, the bootloader "
                                  "may fall back to slot _b (stock)" % args.boot_timeout)
            try:
                if ":" in args.serial:
                    # Wireless adb drops on reboot; reconnect (works only if adbd listens on that port after boot).
                    subprocess.run(["adb", "connect", args.serial], capture_output=True, text=True, timeout=10)
                state = adb("get-state", timeout=10, check=False).strip()
                if state == "device" and adb("shell", "getprop sys.boot_completed", timeout=10,
                                             check=False).strip() == "1":
                    break
            except SwitchError:
                pass
            time.sleep(5)
        new = boot_id()
        cmdline = adb("shell", "cat /proc/cmdline").split()
        slot = adb("shell", "getprop ro.boot.slot_suffix").strip()
        now = sha(PARTITION, timeout=120)
        result.update(new_boot_id=new, slot=slot, boot_a_sha256=now, xhyper_token_present=XHYPER_TOKEN in cmdline,
                      fingerprint=adb("shell", "getprop ro.build.fingerprint").strip(),
                      kernel_runtime_id=sha("/sys/kernel/notes"))
        say("new boot id %s, slot %s, boot_a %s, %s %s" % (new, slot, now[:16], XHYPER_TOKEN,
                                                          "present" if XHYPER_TOKEN in cmdline else "absent"))
        problems = []
        if new == old:
            problems.append("boot id unchanged")
        if slot != "_a":
            problems.append("running from slot %s: the target image did not boot and the bootloader fell back" % slot)
        if now != want:
            problems.append("boot_a is %s after reboot" % now)
        if (XHYPER_TOKEN in cmdline) != (args.mode == "xhyper"):
            problems.append("mode token %s is %s" % (XHYPER_TOKEN, "present" if XHYPER_TOKEN in cmdline else "absent"))
        if problems:
            raise SwitchError("; ".join(problems))
        result["ok"] = True
        say("OK: %s is running" % args.mode)
        return 0
    except (SwitchError, OSError, KeyError, ValueError) as error:
        result.update(ok=False, error=str(error))
        say("FAILED: %s" % error)
        return 1
    finally:
        result["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(base + ".json", "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
