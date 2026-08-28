import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parent.parent
    / "clevo_broker_repetition.py"
).read_text(encoding="utf-8")


class ClevoBrokerRepetitionTests(unittest.TestCase):
    def test_cycle_count_and_colors_are_fixed(self):
        self.assertIn("CYCLE_COUNT = 10", SOURCE)
        self.assertIn("StaticColor(255, 0, 0)", SOURCE)
        self.assertIn("StaticColor(0, 0, 255)", SOURCE)

    def test_each_cycle_requires_clean_stop_and_unchanged_profile(self):
        self.assertIn('"broker_stop_confirmed"', SOURCE)
        self.assertIn('result["profile_after"] != initial_profile', SOURCE)
        self.assertNotIn("retry", SOURCE.casefold())


if __name__ == "__main__":
    unittest.main()
