import subprocess
import sys
import unittest
from unittest.mock import patch

from abbench.device_lock import DeviceLock, serialized


PHYSICAL = "lock-test-T11206"
WIRELESS = "192.0.2.7:5555"


class DeviceLockTests(unittest.TestCase):
    def test_explicit_physical_hint_unifies_transports_without_adb(self):
        with patch("abbench.capture.shell") as shell:
            with DeviceLock(PHYSICAL) as usb, DeviceLock(WIRELESS, PHYSICAL) as wifi:
                self.assertEqual(usb.key, wifi.key)
            shell.assert_not_called()

    def test_unhinted_wireless_resolves_once_while_lock_is_owned(self):
        with patch("abbench.capture.shell", return_value=subprocess.CompletedProcess([], 0, PHYSICAL + "\n", "")) as shell:
            with DeviceLock(WIRELESS) as wifi, DeviceLock(WIRELESS) as nested, DeviceLock(PHYSICAL) as usb:
                self.assertEqual(wifi.key, nested.key)
                self.assertEqual(wifi.key, usb.key)
            self.assertEqual(shell.call_count, 1)
            self.assertEqual(shell.call_args.kwargs["timeout"], 5)
            # Resolved aliases are dropped after release, avoiding stale IP reuse.
            with DeviceLock(WIRELESS):
                pass
            self.assertEqual(shell.call_count, 2)

    def test_wireless_without_real_physical_property_is_rejected(self):
        for value in ("", "unknown", WIRELESS):
            with patch("abbench.capture.shell", return_value=subprocess.CompletedProcess([], 0, value, "")):
                with self.assertRaisesRegex(ValueError, "physical_device_identity_required"):
                    DeviceLock(WIRELESS)

    def test_other_process_cannot_bypass_usb_lock_using_wireless_hint(self):
        with DeviceLock(PHYSICAL):
            proc = subprocess.run([sys.executable, "-c",
                "from abbench.device_lock import DeviceLock; DeviceLock('192.0.2.7:5555', 'lock-test-T11206').__enter__()"],
                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("device_already_locked", proc.stderr)

    def test_decorator_uses_suite_and_campaign_profile_hint(self):
        @serialized
        def fake_suite(serial, directory, mode, processor, profile):
            return "suite"

        @serialized
        def fake_campaign(serial, directory, processor, profile):
            return "campaign"

        with patch("abbench.capture.shell") as shell, DeviceLock(PHYSICAL):
            self.assertEqual(fake_suite(WIRELESS, "out", "native", "tp", {"serial": PHYSICAL}), "suite")
            self.assertEqual(fake_campaign(WIRELESS, "out", "tp", {"serial": PHYSICAL}), "campaign")
            shell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
