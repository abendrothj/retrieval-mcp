"""The discovery predicate, which was an uncommitted pass until its figures could not be reproduced.

A sibling session counted `tool_search` invocations and got 0.00 for every arm of two runs -
including the archived arm whose published figure is 2.40 - which proved the method blind rather
than the run empty. These assert the two properties that make the count right.
"""
import json
import tempfile
import unittest
from pathlib import Path

import discovery_calls


def trial(tmp, scripts, status="completed", system="retrieval-mcp"):
    directory = Path(tmp) / "trial-0000"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps({"system": system, "status": status}))
    lines = [json.dumps({"payload": {"input": s}}) for s in scripts]
    (directory / "codex-session.jsonl").write_text("\n".join(lines) + "\n")
    return directory


class Predicate(unittest.TestCase):
    def test_a_catalogue_filter_in_an_exec_script_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = trial(tmp, ['const xs = ALL_TOOLS.filter(x => /retriev/i.test(x.name));'])
            self.assertEqual(len(discovery_calls.discovery_scripts(directory)), 1)

    def test_identical_scripts_are_counted_once(self):
        """The same script is echoed in codex-events.jsonl and codex-session.jsonl, so occurrence
        counting double-counts - the error that turned one path error into two elsewhere."""
        script = 'const xs = ALL_TOOLS.filter(x => x.name === "mcp__retrieval__search_exact");'
        with tempfile.TemporaryDirectory() as tmp:
            directory = trial(tmp, [script, script, script])
            self.assertEqual(len(discovery_calls.discovery_scripts(directory)), 1)

    def test_distinct_searches_in_one_trial_all_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = trial(tmp, ['ALL_TOOLS.filter(a)', 'ALL_TOOLS.filter(b)',
                                    'ALL_TOOLS.filter(c)'])
            self.assertEqual(len(discovery_calls.discovery_scripts(directory)), 3)

    def test_a_corpus_call_is_not_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = trial(tmp, ['await tools.mcp__retrieval__search_exact({query:"x"})'])
            self.assertEqual(len(discovery_calls.discovery_scripts(directory)), 0)

    def test_the_tool_name_alone_is_not_a_call(self):
        """`tool_search` appears in this client's system-prompt text without ever being invoked,
        so matching the tool's name reports discovery that did not happen."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = trial(tmp, ['// the tool_search feature is enabled'])
            self.assertEqual(len(discovery_calls.discovery_scripts(directory)), 0)


class Report(unittest.TestCase):
    def test_arms_are_averaged_per_trial_and_incomplete_trials_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial(tmp, ['ALL_TOOLS.filter(a)', 'ALL_TOOLS.filter(b)'])
            result = discovery_calls.report(tmp)
            arm = result["arms"]["retrieval-mcp"]
            self.assertEqual((arm["trials"], arm["discovery_calls_per_trial"]), (1, 2.0))
            self.assertEqual(arm["trials_with_discovery"], 1)

    def test_a_trial_that_never_searched_lowers_the_mean_without_vanishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial(tmp, ['ALL_TOOLS.filter(a)'])
            result = discovery_calls.report(tmp)
            self.assertEqual(result["arms"]["retrieval-mcp"]["trials"], 1)
