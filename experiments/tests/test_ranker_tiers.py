"""The matcher behind the tier scheme, which is the part another session had to reimplement.

These assert the two properties that make the tiers mean what they claim: matching is exact and
structural rather than substring over concatenated text, and the three tiers map to the three hop
outcomes. The founding numbers of the scheme - 9 of 12 file, 2 of 12 definition in
runs/semantic-resistant-20260924 - came from an uncommitted pass, which is why this instrument and
this test exist.
"""
import json
import unittest

import ranker_tiers


GOLD = "django/core/management/commands/runserver.py::Command::on_bind"


def row(path, identity=None):
    return {"path": path, "identity": identity}


class Positions(unittest.TestCase):
    def test_an_exact_identity_is_found_with_its_rank(self):
        rows = [row("other.py", "other.py::f"), row(GOLD.split("::")[0], GOLD)]
        self.assertEqual(ranker_tiers.positions(rows, GOLD), (2, 2))

    def test_a_sibling_definition_in_the_right_file_is_file_only(self):
        """The failure django__django-18435 actually showed: the page named `inner_run` and `run`
        in the right file and never `Command::on_bind`."""
        path = GOLD.split("::")[0]
        rows = [row(path, f"{path}::inner_run"), row(path, f"{path}::run")]
        identity, file_rank = ranker_tiers.positions(rows, GOLD)
        self.assertIsNone(identity)
        self.assertEqual(file_rank, 1)
        self.assertEqual(ranker_tiers.tier(identity, file_rank), "file_only")

    def test_a_substring_of_the_gold_identity_does_not_count(self):
        """`end_to_end.unretrieved` is conjunctive over concatenated text, so a gold whose bare
        name appears anywhere in its own file collapses identity_on_page into file_only. Equality
        is what keeps the two tiers distinct."""
        path = GOLD.split("::")[0]
        rows = [row(path, f"{path}::Command::on_bind_later")]
        self.assertEqual(ranker_tiers.positions(rows, GOLD), (None, 1))

    def test_the_right_definition_in_the_wrong_file_is_not_a_hit(self):
        rows = [row("elsewhere.py", "elsewhere.py::Command::on_bind")]
        self.assertEqual(ranker_tiers.positions(rows, GOLD), (None, None))

    def test_an_empty_page_is_neither(self):
        identity, file_rank = ranker_tiers.positions([], GOLD)
        self.assertEqual(ranker_tiers.tier(identity, file_rank), "neither")

    def test_a_row_without_an_identity_still_counts_its_file(self):
        """search_exact rows carry a path and no enclosing definition."""
        rows = [row(GOLD.split("::")[0])]
        self.assertEqual(ranker_tiers.positions(rows, GOLD), (None, 1))


class Tiers(unittest.TestCase):
    def test_each_tier_maps_to_its_hop_outcome(self):
        self.assertEqual(ranker_tiers.tier(1, 1), "identity_on_page")
        self.assertEqual(ranker_tiers.tier(None, 4), "file_only")
        self.assertEqual(ranker_tiers.tier(None, None), "neither")

    def test_identity_wins_over_file_whatever_the_ranks(self):
        self.assertEqual(ranker_tiers.tier(9, 1), "identity_on_page")


class Conditions(unittest.TestCase):
    TASK = {"id": "q1", "question": "[Bug]: thing is broken\n\n### Environment\nlots of prose",
            "expected_json": {"answer": GOLD}}

    def test_the_title_condition_is_the_first_line_only(self):
        """A LOC-BENCH question is a whole issue - one is 16,251 characters against a 16 KiB
        argument limit - so the raw question is not a query any client can send."""
        found = ranker_tiers.conditions_for(self.TASK, {})
        self.assertEqual(found["title"], ["[Bug]: thing is broken"])
        self.assertNotIn("agent", found)

    def test_archived_queries_add_the_agent_condition(self):
        found = ranker_tiers.conditions_for(self.TASK, {"q1": ["cobra command authStatus"]})
        self.assertEqual(found["agent"], ["cobra command authStatus"])

    def test_a_question_with_no_archived_query_gets_no_agent_condition(self):
        self.assertNotIn("agent", ranker_tiers.conditions_for(self.TASK, {"other": ["x"]}))
