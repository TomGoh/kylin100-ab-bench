import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.ui_control import parse_keyguard, prepare_ui

BOOT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def policy(showing=True, secure=False):
    return ("KeyguardServiceDelegate\n  showing=" + str(showing).lower() + "\n  secure=" +
            str(secure).lower() + "\nKeyguardStateMonitor\n  mIsShowing=" + str(showing).lower() + "\n")


class UiPreparationTests(unittest.TestCase):
    def run_fixture(self, policies, *, cross=False, deadline=None):
        now, calls, clock_count = [10.0], [], [0]
        answers = iter(policies)
        def shell(serial, command, timeout):
            calls.append((command, timeout))
            now[0] += 0.1
            if "boot_id" in command:
                clock_count[0] += 1
                boot = OTHER if cross and clock_count[0] > 1 else BOOT
                raw = f"{boot}\n100.0 0.0\n{boot}\n"
            elif command == "dumpsys window policy":
                raw = next(answers)
            else:
                raw = ""
            return subprocess.CompletedProcess([], 0, raw, "")
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "ui"
            with patch("abbench.ui_control.shell", shell), patch("abbench.ui_control.time.monotonic", lambda: now[0]), \
                 patch("abbench.ui_control.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)):
                result = prepare_ui("test-device", out, expected_boot_id=BOOT, deadline_monotonic_s=deadline)
            self.assertEqual(json.loads((out / "ui-control.json").read_text()), result)
            return result, calls

    def test_asynchronous_nonsecure_dismiss_waits_then_goes_home(self):
        result, calls = self.run_fixture([policy(), policy(), policy(False)])
        self.assertTrue(result["valid"])
        self.assertTrue(result["dismiss_requested"])
        self.assertEqual(sum(command == "wm dismiss-keyguard" for command, _ in calls), 1)
        self.assertEqual(calls[-1][0], "input keyevent 3")
        self.assertTrue(all(timeout <= 8 for _, timeout in calls))

    def test_already_unlocked_has_no_dismiss(self):
        result, calls = self.run_fixture([policy(False), policy(False)])
        self.assertTrue(result["valid"])
        self.assertFalse(any(command == "wm dismiss-keyguard" for command, _ in calls))

    def test_secure_and_unknown_policy_never_attempt_dismiss(self):
        for raw in (policy(secure=True), "KeyguardServiceDelegate\nshowing=true\n"):
            result, calls = self.run_fixture([raw])
            self.assertFalse(result["valid"])
            self.assertFalse(any(command.startswith("input") or command == "wm dismiss-keyguard" for command, _ in calls))

    def test_cross_boot_or_expired_budget_is_invalid(self):
        result, _ = self.run_fixture([policy(False), policy(False)], cross=True)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "device_restarted_during_ui_prepare")
        result, calls = self.run_fixture([], deadline=10)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "deadline_exhausted")
        self.assertEqual(calls, [])

    def test_policy_requires_delegate_and_monitor_to_agree_for_clear_state(self):
        raw = policy(False).replace("mIsShowing=false", "mIsShowing=true")
        parsed = parse_keyguard(raw)
        self.assertFalse(parsed["showing"])
        self.assertTrue(parsed["monitor_showing"])


if __name__ == "__main__":
    unittest.main()
