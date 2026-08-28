import unittest

import diagnostics.clevo_dchu_blue_pulse as pulse


class ClevoDchuBluePulseTests(unittest.TestCase):
    def test_allowlist_is_color_command_and_three_segments_only(self):
        self.assertEqual(pulse.EXPECTED_DCHU_COMMAND, 103)
        self.assertEqual(pulse.EXPECTED_SEGMENTS, (0xF0, 0xF1, 0xF2))

    def test_pulse_is_short(self):
        self.assertLessEqual(pulse.PULSE_MILLISECONDS, 2000)

    def test_source_has_no_persistent_write_or_profile_command(self):
        self.assertNotIn("WriteAppSettings", pulse.SOURCE)
        self.assertNotIn("setDchu(121", pulse.SOURCE)
        self.assertIn("setDchu(103", pulse.SOURCE)


if __name__ == "__main__":
    unittest.main()
