import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parent.parent
    / "clevo_dchu_103_readback_probe.py"
).read_text(encoding="utf-8")


class ClevoDchu103ReadbackProbeTests(unittest.TestCase):
    def test_probe_is_fixed_and_read_only(self):
        self.assertIn("getBuffer(103, buffer)", SOURCE)
        self.assertIn("GetDCHU_Data_Buffer", SOURCE)
        self.assertNotIn("SetDCHU_Data", SOURCE)
        self.assertNotIn("WriteAppSettings", SOURCE)

    def test_probe_has_bounded_wait(self):
        self.assertIn("complete.wait(3.0)", SOURCE)


if __name__ == "__main__":
    unittest.main()
