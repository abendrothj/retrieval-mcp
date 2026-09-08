import unittest

from quality_pass import DECLINE, answer_json, credit, parse_symbol, resolve

INDEX = {"break_lines": {"src/uu/fmt/src/linebreak.rs"},
         "render": {"src/uu/dd/src/diagnostics.rs", "src/uu/ls/src/render.rs"},
         "locate_operand": {"src/uucore/src/lib/features/diagnostics.rs"}}


class ResolverTests(unittest.TestCase):
    def test_notation_does_not_matter_when_the_name_is_unique(self):
        for written in ("linebreak::break_lines", "src/uu/fmt/src/linebreak.rs::break_lines", "break_lines"):
            self.assertEqual(resolve(written, INDEX)[0], "src/uu/fmt/src/linebreak.rs", written)

    def test_an_ambiguous_name_needs_the_context_the_answer_supplied(self):
        self.assertIsNone(resolve("render", INDEX), "a name in two files must not resolve on its own")
        self.assertEqual(resolve("uu_dd::diagnostics::render", INDEX)[0], "src/uu/dd/src/diagnostics.rs")

    def test_a_type_qualifier_is_kept(self):
        self.assertEqual(parse_symbol("uucore::diagnostics::Snapshot::locate_operand"),
                         ("Snapshot", "locate_operand"))

    def test_unknown_names_do_not_resolve(self):
        self.assertIsNone(resolve("nonexistent_symbol", INDEX))


class CreditTests(unittest.TestCase):
    def test_sets_score_by_overlap_and_are_penalised_for_extras(self):
        gold = ["src/uu/fmt/src/linebreak.rs::break_lines", "src/uu/dd/src/diagnostics.rs::render"]
        self.assertEqual(credit(["linebreak::break_lines", "uu_dd::diagnostics::render"], gold, INDEX), 1.0)
        self.assertEqual(credit(["linebreak::break_lines"], gold, INDEX), 0.5)
        self.assertEqual(credit(["linebreak::break_lines", "uu_dd::diagnostics::render",
                                 "src/other.rs::extra"], gold, INDEX), 0.5)

    def test_null_gold_rewards_only_abstention(self):
        self.assertEqual(credit(None, None, INDEX), 1.0)
        self.assertEqual(credit("something", None, INDEX), 0.0)

    def test_answer_is_read_from_a_fenced_block_or_a_bare_object(self):
        self.assertEqual(answer_json('```json\n{"answer": 3}\n```'), 3)
        self.assertEqual(answer_json('{"answer": 3}'), 3)
        self.assertIsNone(answer_json("no answer here"))

    def test_decline_wording_is_detected(self):
        self.assertTrue(DECLINE.search("I cannot determine this without retrieval"))
        self.assertFalse(DECLINE.search("The answer is NumInfo::parse"))


if __name__ == "__main__":
    unittest.main()
