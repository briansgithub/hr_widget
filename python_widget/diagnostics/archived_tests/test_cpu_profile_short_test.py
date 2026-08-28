import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parent.parent
    / "cpu_profile_short_test.py"
).read_text(encoding="utf-8")


class CpuProfileShortTestTests(unittest.TestCase):
    def test_load_and_sampling_are_bounded(self):
        self.assertIn("WORKERS = 12", SOURCE)
        self.assertIn("RAMP_SECONDS = 2.0", SOURCE)
        self.assertIn("SAMPLE_COUNT = 6", SOURCE)
        self.assertIn("worker.join(timeout=1.0)", SOURCE)
        self.assertIn("_start_counter_stream()", SOURCE)
        self.assertIn("_next_counter_sample(counter_output)", SOURCE)

    def test_temperature_profile_and_limit_gates_are_present(self):
        self.assertIn("args.starting_temperature_c > 65.0", SOURCE)
        self.assertIn("initial_profile != 2", SOURCE)
        self.assertIn('sample["performance_limit_flags"] != 0', SOURCE)
        self.assertIn("KNOWN_GOOD_PERFORMANCE_MHZ * 0.85", SOURCE)


if __name__ == "__main__":
    unittest.main()
