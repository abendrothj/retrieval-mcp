"""What a suite can test, asserted where it is easy to get wrong.

The failure this guards against is a registration promising a margin the design cannot reach -
the shape of "quality ties" reported three times in this record from designs sitting at their
ceiling. So the properties here are the ones that keep the answer conservative: the exact
binomial is right, power rises with the margin and with the sample, a ceiling is reported as a
ceiling, and a margin the design cannot test exits nonzero.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import power

EXPERIMENTS = Path(__file__).resolve().parents[1]


class Survival(unittest.TestCase):
    def test_against_values_computed_by_hand(self):
        self.assertAlmostEqual(power.survival(1, 1, 0.5), 0.5)
        self.assertAlmostEqual(power.survival(2, 2, 0.5), 0.25)
        # P(X >= 1) for three fair draws is 1 - (1/2)^3.
        self.assertAlmostEqual(power.survival(1, 3, 0.5), 0.875)

    def test_the_bounds_are_certainties(self):
        self.assertEqual(power.survival(0, 10, 0.3), 1.0)
        self.assertEqual(power.survival(11, 10, 0.3), 0.0)

    def test_a_certain_process_is_certain(self):
        self.assertAlmostEqual(power.survival(10, 10, 1.0), 1.0)


class CriticalValue(unittest.TestCase):
    def test_it_is_the_first_outcome_that_rejects(self):
        trials, null, alpha = 20, 0.5, 0.05
        found = power.critical_value(trials, null, alpha)
        self.assertLessEqual(power.survival(found, trials, null), alpha)
        self.assertGreater(power.survival(found - 1, trials, null), alpha)

    def test_a_suite_too_small_to_reject_says_so(self):
        """`trials + 1` means no outcome rejects. A real answer for a small design."""
        self.assertEqual(power.critical_value(2, 0.95, 0.05), 3)


class ReachableMargin(unittest.TestCase):
    def test_more_trials_test_a_tighter_margin(self):
        small = power.reachable_margin(30, 0.95)
        large = power.reachable_margin(300, 0.95)
        self.assertIsNotNone(small)
        self.assertIsNotNone(large)
        self.assertLess(large, small)

    def test_the_margin_it_returns_has_the_target_power_and_the_one_below_does_not(self):
        trials, rate = 90, 0.95
        margin = power.reachable_margin(trials, rate)
        self.assertGreaterEqual(power.power_for(trials, rate, margin, power.ALPHA), 0.8)
        self.assertLess(power.power_for(trials, rate, margin - power.STEP, power.ALPHA), 0.8)

    def test_power_rises_with_the_margin(self):
        rising = [power.power_for(90, 0.95, margin, power.ALPHA)
                  for margin in (0.02, 0.05, 0.10, 0.20)]
        self.assertEqual(rising, sorted(rising))


class Headroom(unittest.TestCase):
    def test_a_control_at_its_ceiling_leaves_no_win_to_measure(self):
        """62 of 63, the kernel baseline: no quality win is available at any sample size."""
        room = power.headroom(63, 62 / 63)
        self.assertLess(room["trials_the_control_loses"], 2)
        self.assertFalse(room["a_win_is_measurable"])

    def test_a_control_with_room_says_so(self):
        self.assertTrue(power.headroom(90, 0.90)["a_win_is_measurable"])


class PositiveControl(unittest.TestCase):
    def test_a_knob_that_cannot_move_more_than_the_margin_is_not_powered(self):
        """Django at --page-fraction 0.5: 3 of 30 questions, one repetition."""
        floor = power.control_floor(30, 0.96, at_risk_trials=3)
        self.assertEqual(floor["at_risk_trials"], 3)
        self.assertFalse(floor["powered_at_the_ceiling"])

    def test_a_knob_that_moves_more_than_the_margin_is_powered(self):
        floor = power.control_floor(90, 0.96, at_risk_trials=45)
        self.assertTrue(floor["powered_at_the_ceiling"])

    def test_no_control_supplied_is_not_an_answer_of_zero(self):
        self.assertIsNone(power.control_floor(90, 0.96, at_risk_trials=None))

    def test_at_risk_questions_are_carried_into_trials_by_the_repetitions(self):
        result = power.report(30, 3, 0.96, at_risk=9)
        self.assertEqual(result["positive_control"]["at_risk_trials"], 27)


class CommandLine(unittest.TestCase):
    def run_it(self, *extra):
        return subprocess.run(
            [sys.executable, str(EXPERIMENTS / "power.py"), "--questions", "30",
             "--repetitions", "3", "--control-rate", "0.96", *extra],
            capture_output=True, text=True)

    def test_a_margin_the_design_cannot_test_exits_nonzero(self):
        done = self.run_it("--margin", "0.001")
        self.assertEqual(done.returncode, 1)
        self.assertIn("REFUSED", done.stdout)

    def test_a_margin_the_design_can_test_exits_zero(self):
        done = self.run_it("--margin", "0.5")
        self.assertEqual(done.returncode, 0)
        self.assertIn("OK", done.stdout)

    def test_it_writes_a_block_for_the_preregistration(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "power.json"
            done = self.run_it("--output", str(out))
            self.assertEqual(done.returncode, 0, done.stderr)
            block = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(block["trials"], 90)
        self.assertIn("reachable_non_inferiority_margin", block)

    def test_an_impossible_control_rate_is_refused(self):
        done = subprocess.run(
            [sys.executable, str(EXPERIMENTS / "power.py"), "--questions", "30",
             "--control-rate", "1.5"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
