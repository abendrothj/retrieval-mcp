import unittest

from failure_modes import classify

INDEX = {"break_lines": {"src/uu/fmt/src/linebreak.rs"}, "render": {"src/uu/dd/src/diagnostics.rs"}}
GOLD = ["src/uu/fmt/src/linebreak.rs::break_lines", "src/uu/dd/src/diagnostics.rs::render"]
WANTED = {"src/uu/fmt/src/linebreak.rs": {39, 89}, "src/uu/dd/src/diagnostics.rs": {52}}
SAW = {"src/uu/fmt/src/linebreak.rs": set(range(30, 100)), "src/uu/dd/src/diagnostics.rs": {52}}


class ClassifyTests(unittest.TestCase):
    def test_uncovered_evidence_outranks_everything(self):
        self.assertEqual(classify(["linebreak::break_lines"], GOLD, {}, WANTED, INDEX), "never_saw_evidence")
        near_miss = {"src/uu/fmt/src/linebreak.rs": {1, 2, 3}}
        self.assertEqual(classify(["x"], GOLD, near_miss, WANTED, INDEX), "never_saw_evidence")

    def test_right_but_short_is_incomplete(self):
        self.assertEqual(classify(["linebreak::break_lines"], GOLD, SAW, WANTED, INDEX), "incomplete")
        self.assertEqual(classify(None, GOLD, SAW, WANTED, INDEX), "incomplete")

    def test_a_wrong_member_is_a_misread_not_an_omission(self):
        self.assertEqual(classify(["linebreak::break_lines", "src/other.rs::wrong"], GOLD, SAW, WANTED, INDEX),
                         "misread")
        self.assertEqual(classify("src/other.rs::wrong", "src/uu/fmt/src/linebreak.rs::break_lines",
                                  SAW, WANTED, INDEX), "misread")


if __name__ == "__main__":
    unittest.main()
