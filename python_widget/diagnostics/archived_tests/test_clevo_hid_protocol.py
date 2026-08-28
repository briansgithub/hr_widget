import unittest

from clevo_hid_protocol import (
    ClevoHidProtocolError,
    FEATURE_REPORT_LENGTH,
    QUALIFICATION_KEY_IDS,
    encode_key_id,
    encode_static_key_color,
    format_report,
    physical_rgb_to_wire,
)


class ClevoHidProtocolTests(unittest.TestCase):
    def test_key_id_encoding_matches_documented_matrix(self):
        self.assertEqual(encode_key_id(0, 0), 0x00)
        self.assertEqual(encode_key_id(3, 2), QUALIFICATION_KEY_IDS["A"])
        self.assertEqual(encode_key_id(5, 19), 0xB3)

    def test_physical_red_and_green_are_swapped_at_wire_boundary(self):
        self.assertEqual(physical_rgb_to_wire((255, 0, 0)), (0, 255, 0))
        self.assertEqual(physical_rgb_to_wire((0, 255, 0)), (255, 0, 0))
        self.assertEqual(physical_rgb_to_wire((0, 0, 255)), (0, 0, 255))

    def test_one_key_report_is_exactly_seven_bytes_with_zero_trailer(self):
        report = encode_static_key_color(QUALIFICATION_KEY_IDS["A"], (255, 0, 0))
        self.assertEqual(report, bytes.fromhex("CC 01 62 00 FF 00 00"))
        self.assertEqual(len(report), FEATURE_REPORT_LENGTH)

    def test_blue_dry_run_format(self):
        report = encode_static_key_color(QUALIFICATION_KEY_IDS["A"], (0, 0, 255))
        self.assertEqual(format_report(report), "CC 01 62 00 00 FF 00")

    def test_rejects_out_of_range_matrix_coordinates(self):
        for row, column in ((-1, 0), (6, 0), (0, -1), (0, 20)):
            with self.subTest(row=row, column=column):
                with self.assertRaises(ClevoHidProtocolError):
                    encode_key_id(row, column)

    def test_rejects_malformed_color_and_values(self):
        bad_colors = ((0, 0), (0, 0, 0, 0), (-1, 0, 0), (0, 256, 0), (True, 0, 0))
        for color in bad_colors:
            with self.subTest(color=color):
                with self.assertRaises(ClevoHidProtocolError):
                    encode_static_key_color(0x62, color)

        with self.assertRaises(ClevoHidProtocolError):
            encode_static_key_color(0x100, (0, 0, 0))

    def test_encoder_exposes_no_brightness_or_effect_commands(self):
        import clevo_hid_protocol

        exported_names = set(vars(clevo_hid_protocol))
        self.assertNotIn("encode_brightness", exported_names)
        self.assertNotIn("encode_effect", exported_names)
        self.assertNotIn("encode_save_bios", exported_names)


if __name__ == "__main__":
    unittest.main()
