"""Convergence is the whole discipline of the prefix instrument, so it is what gets tested."""
import unittest

import prompt_prefix


def runs(*totals):
    return [{"input_tokens": n, "cache_read": 0, "output_tokens": 5} for n in totals]


class ConvergenceTests(unittest.TestCase):
    def test_a_settled_block_reports_its_settled_value(self):
        self.assertEqual(prompt_prefix.converged(runs(15555, 4547, 451, 451)), 451)

    def test_a_block_still_falling_reports_nothing(self):
        """The trap this instrument exists for. 15,555 -> 4,547 is the cache draining, not a
        prefix, and reporting 4,547 as the answer is how `--disable shell_tool` was once
        measured at +9,600 tokens a request."""
        self.assertIsNone(prompt_prefix.converged(runs(15555, 4547)))

    def test_two_percent_of_drift_is_still_settled(self):
        self.assertEqual(prompt_prefix.converged(runs(10000, 10150)), 10150)

    def test_more_than_two_percent_is_not(self):
        self.assertIsNone(prompt_prefix.converged(runs(10000, 10500)))

    def test_one_run_cannot_converge(self):
        self.assertIsNone(prompt_prefix.converged(runs(451)))

    def test_a_zero_reading_is_not_a_measurement(self):
        self.assertIsNone(prompt_prefix.converged(runs(0, 0)))


if __name__ == "__main__":
    unittest.main()
