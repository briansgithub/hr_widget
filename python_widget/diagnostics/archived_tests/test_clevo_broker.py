import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "clevo_broker" / "Program.cs").read_text(encoding="utf-8")
MANIFEST = (ROOT / "clevo_broker" / "Package" / "AppxManifest.xml").read_text(encoding="utf-8")


class ClevoBrokerTests(unittest.TestCase):
    def test_probe_stays_read_only_and_writes_are_lighting_only(self):
        self.assertIn('"ReadAppSettings"', SOURCE)
        for forbidden in (
            "WriteAppSettings",
            "SetPowerMode",
            "LightingCommand = 121",
            "SetLightingData(121",
            "taskkill",
            "Stop-Process",
            "Add-AppxPackage",
        ):
            self.assertNotIn(forbidden, SOURCE)
        self.assertIn("private const int LightingCommand = 103", SOURCE)
        self.assertIn('args[0] != "--probe" && args[0] != "--serve"', SOURCE)

    def test_only_fixed_probe_argument_is_accepted(self):
        self.assertIn('"--probe"', SOURCE)
        self.assertNotRegex(SOURCE, r"int\.Parse|Convert\.FromHexString|args\[[^0]\]")

    def test_exact_oem_library_is_hash_allowlisted(self):
        self.assertIn("CLEVOCO.LedKeyboardSetting_1.14.0.0_x64__6h6z29zh29qx0", SOURCE)
        self.assertIn("98AF9793B911AB57FCBD09FD75441A120A19A529F4D18D03D83BE42DE7020A47", SOURCE)

    def test_manifest_is_separately_named_and_medium_integrity(self):
        self.assertIn('Name="HRWidget.ClevoBroker.Experimental"', MANIFEST)
        self.assertIn('uap10:TrustLevel="mediumIL"', MANIFEST)
        self.assertIn('Alias="hrclevobroker-exp.exe"', MANIFEST)
        self.assertNotIn("CLEVOCO", MANIFEST)

    def test_exact_hardware_and_oem_lifecycle_gates_are_embedded(self):
        self.assertIn('HardwareIdentity = "Micro Electronics Inc|1520|PB50EF-G"', SOURCE)
        self.assertIn("StaticAllColorMode = 8", SOURCE)
        self.assertIn('new[] { "CCDCHUService", "HKClipSvc" }', SOURCE)
        self.assertIn("FnKey identity changed during the lighting lease", SOURCE)
        self.assertIn("OEM_LIGHTING_RECLAIMED", SOURCE)

    def test_no_false_hardware_readback_claim(self):
        self.assertIn("synchronous_vendor_return_103", SOURCE)
        self.assertIn("No hardware color or brightness readback is available", SOURCE)
        self.assertNotIn("readback_rgb", SOURCE)

    def test_brightness_is_limited_to_observed_oem_levels(self):
        self.assertIn('type == "SET_BRIGHTNESS"', SOURCE)
        self.assertIn('RequireInt(body, "level", 0, 4)', SOURCE)
        self.assertIn("level * 48 - 1", SOURCE)
        self.assertIn("byte[] offFrame = [0x07, 0, 0, 0xE0]", SOURCE)
        self.assertIn("byte[] statusFrame = [0x01, 0xB0, 0x07, 0xE0]", SOURCE)

    def test_scheduler_jitter_cannot_quarantine_the_broker(self):
        self.assertNotIn("RATE_LIMIT", SOURCE)
        self.assertNotIn("lastApplyTicks", SOURCE)
        self.assertIn("one synchronous command at a time", SOURCE)


if __name__ == "__main__":
    unittest.main()
