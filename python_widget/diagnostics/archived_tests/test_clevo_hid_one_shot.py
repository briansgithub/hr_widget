import unittest

from clevo_hid_one_shot import (
    MATRIX_WORKER_TIMEOUT_SECONDS,
    QUALIFICATION_REPORT,
    QUALIFICATION_REPORT_HEX,
    WHITE_MATRIX_REPORTS,
    WORKER_TIMEOUT_SECONDS,
)


class ClevoHidOneShotTests(unittest.TestCase):
    def test_only_payload_is_a_key_physical_blue(self):
        self.assertEqual(QUALIFICATION_REPORT, bytes.fromhex(QUALIFICATION_REPORT_HEX))
        self.assertEqual(QUALIFICATION_REPORT_HEX, "CC 01 62 00 00 FF 00")

    def test_worker_deadline_is_short(self):
        self.assertLessEqual(WORKER_TIMEOUT_SECONDS, 2.0)
        self.assertLessEqual(MATRIX_WORKER_TIMEOUT_SECONDS, 5.0)

    def test_white_matrix_is_one_pass_of_set_led_only(self):
        self.assertEqual(len(WHITE_MATRIX_REPORTS), 120)
        self.assertEqual(WHITE_MATRIX_REPORTS[0], bytes.fromhex("CC 01 00 FF FF FF 00"))
        self.assertEqual(WHITE_MATRIX_REPORTS[-1], bytes.fromhex("CC 01 B3 FF FF FF 00"))
        self.assertTrue(all(report[1] == 0x01 for report in WHITE_MATRIX_REPORTS))
        self.assertTrue(all(report[3:6] == b"\xff\xff\xff" for report in WHITE_MATRIX_REPORTS))


if __name__ == "__main__":
    unittest.main()
