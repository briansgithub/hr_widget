import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parent.parent
    / "clevo_broker_blue_pulse.py"
).read_text(encoding="utf-8")


class ClevoBrokerBluePulseTests(unittest.TestCase):
    def test_single_fixed_color_and_no_retry(self):
        self.assertEqual(SOURCE.count('"type": "SET_COLOR"'), 1)
        self.assertIn('"rgb": {"r": 0, "g": 0, "b": 255}', SOURCE)
        self.assertNotIn("retry", SOURCE.casefold().replace("no retry", ""))

    def test_bounded_protocol_and_broker_only_termination(self):
        self.assertIn("TIMEOUT_SECONDS = 2.0", SOURCE)
        self.assertIn("process.kill()", SOURCE)
        self.assertNotIn("FnKey", SOURCE)
        self.assertNotIn("taskkill", SOURCE.casefold())


if __name__ == "__main__":
    unittest.main()
