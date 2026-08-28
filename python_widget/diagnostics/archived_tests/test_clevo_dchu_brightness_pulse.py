import unittest

import diagnostics.clevo_dchu_brightness_pulse as pulse


class ClevoDchuBrightnessPulseTests(unittest.TestCase):
    def test_allowlist_is_exact(self):
        self.assertEqual(pulse.EXPECTED_DCHU_COMMAND, 103)
        self.assertEqual(pulse.TARGET_VALUE, 95)
        self.assertEqual(pulse.EXPECTED_START_VALUE, 143)
        self.assertEqual(pulse.STATUS_FRAME, (0x01, 0xB0, 0x07, 0xE0))

    def test_pulse_is_short(self):
        self.assertLessEqual(pulse.PULSE_MILLISECONDS, 2000)

    def test_source_has_no_persistent_write_or_profile_command(self):
        self.assertNotIn("WriteAppSettings", pulse.SOURCE)
        self.assertNotIn("setDchu(121", pulse.SOURCE)
        self.assertIn("setDchu(103", pulse.SOURCE)


if __name__ == "__main__":
    unittest.main()
