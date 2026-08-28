import unittest

import clevo_dchu_read_probe


class ClevoDchuReadProbeTests(unittest.TestCase):
    def test_fixed_read_fields_are_narrow_and_complete(self):
        self.assertEqual(
            clevo_dchu_read_probe.READ_FIELDS,
            (
                ("cpu_profile", 1, 1, 1),
                ("led_status", 2, 80, 1),
                ("led_rgb", 2, 81, 3),
                ("led_brightness", 2, 35, 1),
            ),
        )

    def test_deadline_is_short(self):
        self.assertLessEqual(clevo_dchu_read_probe.WORKER_TIMEOUT_SECONDS, 2.0)

    def test_module_exposes_no_write_function(self):
        names = set(vars(clevo_dchu_read_probe))
        self.assertNotIn("set_dchu_data", names)
        self.assertNotIn("write_app_settings", names)


if __name__ == "__main__":
    unittest.main()
