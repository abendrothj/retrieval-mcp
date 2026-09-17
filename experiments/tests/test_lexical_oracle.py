import unittest

import lexical_oracle


class QuestionTermTests(unittest.TestCase):
    def test_gold_identifiers_are_never_queried_and_prose_is_ranked_last(self):
        question = ("Identify every definition that calls the helper described here, which the "
                    "`registry` module exposes as lazy_model_operation.")
        terms = lexical_oracle.question_terms(question, {"lazy_model_operation"})
        self.assertNotIn("lazy_model_operation", terms)
        self.assertEqual(terms[0], "registry")
        self.assertNotIn("definition", terms)


class ExposureTests(unittest.TestCase):
    TASK = {"expected_json": {"answer": ["pkg/a.py::alpha", "pkg/b.py::beta"]}}

    def test_an_identity_needs_both_its_path_and_its_leaf(self):
        pairs = lexical_oracle.identities(self.TASK)
        self.assertEqual(lexical_oracle.exposed("pkg/a.py alpha", pairs), [("pkg/a.py", "alpha")])
        self.assertEqual(lexical_oracle.exposed("alpha beta", pairs), [])


class LowerBoundTests(unittest.TestCase):
    def test_one_read_per_gold_file_plus_the_search_that_found_them(self):
        task = {
            "expected_json": {"answer": ["pkg/a.py::alpha", "pkg/a.py::gamma", "pkg/b.py::beta"]},
            "tool_path": [{"tool": "search_concept"}, {"tool": "find_callers"}],
        }
        self.assertEqual(lexical_oracle.lower_bound(task), {
            "gold_identities": 3,
            "gold_files": 2,
            "minimum_lexical_calls": 3,
            "structural_calls_in_proven_path": 2,
        })


class CrawlPolicyTests(unittest.TestCase):
    TASK = {"question": "who calls the registry helper", "tool_path": [],
            "expected_json": {"answer": ["pkg/a.py::alpha"]}}

    def test_a_baseline_crawl_may_enumerate_callers_and_a_lexical_one_may_not(self):
        for extra, expected in ((("find_callers",), "find_callers"), ((), "read_source")):
            crawl = lexical_oracle.Crawl(self.TASK, budget=8, window=120, extra_tools=extra)
            crawl.absorb("search_concept", {}, {"results": [{"path": "pkg/a.py", "line": 4,
                                                             "name": "helper"}]})
            crawl.terms.clear()
            tool, _ = crawl.next_action(1)
            self.assertEqual(tool, expected)


if __name__ == "__main__":
    unittest.main()
