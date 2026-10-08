import copy
import unittest
from abbench.supply import parse_supply_output, verify_supply_window, verify_supply_snapshot

BOOT = "11111111-1111-4111-8111-111111111111"

def raw(start=90, end=91, *, boot=BOOT, serial="test-tablet", usb="0", powered="false", extra=""):
    return (f"ABSUPPLY\t1\nBEGIN\t{boot}\t{serial}\t{start}\n"
            "NODE\t/sys/class/power_supply/battery\tBattery\t1\t1\n"
            f"NODE\t/sys/class/power_supply/usb\tUSB\t{usb}\t?\n"
            f"{extra}DUMP_BEGIN\nCurrent Battery Service state:\n"
            f"  AC powered: false\n  USB powered: {powered}\n"
            "  Wireless powered: false\n  Dock powered: false\nDUMP_END\n"
            f"END\t{boot}\t{serial}\t{end}\n")

class SupplyTests(unittest.TestCase):
    def test_single_snapshot_checks_actual_physical_identity_without_duration(self):
        snapshot=parse_supply_output(raw())
        self.assertTrue(verify_supply_snapshot(snapshot,physical_serial="test-tablet")["verified_off"])
        self.assertFalse(verify_supply_snapshot(snapshot,physical_serial="other-tablet")["verified_off"])
        self.assertFalse(verify_supply_snapshot({"raw":raw(usb="1")},physical_serial="test-tablet")["verified_off"])

    def test_missing_online_auxiliary_roles_require_evidence_and_inventory_binding(self):
        text=raw(extra="NODE\t/sys/class/power_supply/gauge\tUnknown\t?\t1\n")
        snapshot=parse_supply_output(text)
        self.assertTrue(snapshot["valid"])
        self.assertIn("dumpsys_powered",snapshot)
        self.assertFalse(snapshot["all_external_off"])
        profile={"serial":"test-tablet","supply_inventory_sha256":snapshot["supply_inventory_sha256"],
                 "auxiliary_supply_paths":[{"path":"/sys/class/power_supply/gauge","type":"Unknown",
                                             "role":"battery_gauge","evidence_reference":"verified-driver-role.json"}]}
        checked=verify_supply_snapshot(snapshot,physical_serial="test-tablet",profile=profile)
        self.assertTrue(checked["verified_off"])
        post={"raw":raw(121,122,extra="NODE\t/sys/class/power_supply/gauge\tUnknown\t?\t1\n")}
        self.assertTrue(verify_supply_window(snapshot,post,boot_id=BOOT,physical_serial="test-tablet",start_s=110,end_s=120,profile=profile)["verified_off"])
        for change in ({"serial":"wrong"},{"supply_inventory_sha256":"0"*64}):
            self.assertFalse(verify_supply_snapshot(snapshot,physical_serial="test-tablet",profile=dict(profile,**change))["verified_off"])
        for key,value in (("evidence_reference",""),("type","Mains"),("role","ignored"),("path","/sys/class/power_supply/not-present")):
            bad=copy.deepcopy(profile)
            bad["auxiliary_supply_paths"][0][key]=value
            self.assertFalse(verify_supply_snapshot(snapshot,physical_serial="test-tablet",profile=bad)["verified_off"])
        added={"raw":text.replace("DUMP_BEGIN", "NODE\t/sys/class/power_supply/new-input\tUnknown\t?\t?\nDUMP_BEGIN")}
        self.assertFalse(verify_supply_snapshot(added,physical_serial="test-tablet",profile=profile)["verified_off"])
        for online in ("0","1"):
            has_online={"raw":text.replace("Unknown\t?\t1", "Unknown\t"+online+"\t1")}
            self.assertFalse(verify_supply_snapshot(has_online,physical_serial="test-tablet",profile=profile)["verified_off"])

    def test_unknown_type_with_online_remains_a_possible_external_source(self):
        self.assertTrue(verify_supply_snapshot({"raw":raw().replace("USB\t0", "Unknown\t0")},physical_serial="test-tablet")["verified_off"])
        self.assertFalse(verify_supply_snapshot({"raw":raw().replace("USB\t0", "Unknown\t1")},physical_serial="test-tablet")["verified_off"])

    def check(self, pre=None, post=None):
        return verify_supply_window(pre or parse_supply_output(raw()),
            post or parse_supply_output(raw(121,122)), boot_id=BOOT,
            physical_serial="test-tablet", start_s=110,end_s=120)

    def test_off_sources_bracket_window_battery_online_is_not_external(self):
        result=self.check()
        self.assertTrue(result["verified_off"])
        self.assertFalse(result["sensor_calibrated"])
        self.assertEqual(result["observations"][0]["sysfs_sources"][0]["external_source"],False)

    def test_true_contradictory_unknown_missing_and_simulated_are_rejected(self):
        cases=[raw(usb="1"),raw(powered="true"),raw(usb="?"),
               raw().replace("  Dock powered: false\n", ""),
               raw().replace("Current Battery Service state:","UPDATES STOPPED -- use reset"),
               raw().replace("USB\t0", "Unknown\t?"),
               raw().replace("Battery\t1\t1", "Battery\t1\t?")]
        for text in cases:
            with self.subTest(raw=text):
                self.assertFalse(self.check(pre={"raw":text})["verified_off"])

    def test_cross_boot_identity_and_partial_span_are_rejected(self):
        cases=[raw(boot="22222222-2222-4222-8222-222222222222"),
               raw(serial="other-tablet"),raw(110,111),raw(90,float("nan"))]
        for text in cases:
            with self.subTest(raw=text):
                self.assertFalse(self.check(pre={"raw":text})["verified_off"])
        self.assertFalse(self.check(post={"raw":raw(119,122)})["verified_off"])
        self.assertFalse(verify_supply_window(None,None,boot_id=BOOT,physical_serial="test-tablet",start_s=110,end_s=120)["verified_off"])
        for start in (True, float("nan"), -1, 10**1000):
            self.assertFalse(verify_supply_window({"raw":raw()},{"raw":raw(121,122)},
                             boot_id=BOOT,physical_serial="test-tablet",start_s=start,end_s=120)["verified_off"])

    def test_inventory_must_be_stable_and_flags_cannot_override_raw(self):
        post={"raw":raw(121,122,extra="NODE\t/sys/class/power_supply/ac\tMains\t0\t?\n")}
        self.assertEqual(self.check(post=post)["reason"],"supply_inventory_changed")
        forged=parse_supply_output(raw(usb="1"))
        forged.update(all_external_off=True,valid=True)
        self.assertFalse(self.check(pre=forged)["verified_off"])

if __name__ == "__main__":
    unittest.main()
