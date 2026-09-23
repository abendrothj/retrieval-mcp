"""Mining a suite from upstream commits, with the filters that keep the sample honest.

The point of this instrument is that no agent in this repository chose the questions, so the
properties worth asserting are the ones that could quietly reintroduce a choice: that a filter
looks at the corpus and never at an arm, that a message spelling its own answer is refused
because `validate_suite.py` would refuse it anyway, that `names_target` is recorded rather than
cut, and that what comes out actually compiles.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import commit_suite
import validate_suite

HEAD_PY = """\
def parse_header(text):
    parts = text.split(":")
    return parts[0]


def render_footer(rows):
    return "\\n".join(rows)


def unrelated_helper(value):
    return value
"""


def run(repo, *arguments):
    done = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"git {arguments}: {done.stderr}")
    return done.stdout


def commit(repo, message):
    run(repo, "add", "-A")
    run(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)
    return run(repo, "rev-parse", "HEAD").strip()


def make_repo(directory):
    """A corpus at a pinned revision, then history after it - one commit per filter under test."""
    repo = Path(directory) / "corpus"
    repo.mkdir()
    run(repo, "init", "-q", "-b", "main")
    source = repo / "app.py"
    source.write_text(HEAD_PY, encoding="utf-8")
    pinned = commit(repo, "initial import")

    # A describable change to one definition, whose message never spells the name.
    source.write_text(HEAD_PY.replace('parts = text.split(":")',
                                      'parts = text.split(":", 1)'), encoding="utf-8")
    commit(repo, "Split the leading field only\n\nSplitting on every colon lost the remainder "
                 "of a value that legitimately contained one, so the first separator now bounds "
                 "the split and the rest of the value is preserved intact for the caller.")

    # Whitespace only: must produce no hunks under -w and therefore no question.
    source.write_text(source.read_text(encoding="utf-8").replace(
        "def render_footer(rows):", "def render_footer(rows):  "), encoding="utf-8")
    commit(repo, "Trailing whitespace tidy-up\n\n" + "Purely cosmetic reformatting with no "
                 "behavioural change whatsoever in this module, applied across the file.")

    # A message that spells its own target: refused, because validate_suite would refuse it.
    source.write_text(source.read_text(encoding="utf-8").replace(
        'return "\\n".join(rows)', 'return "\\n".join(r for r in rows if r)'), encoding="utf-8")
    commit(repo, "Drop empty rows in render_footer\n\nThe function render_footer emitted blank "
                 "lines for empty entries, which downstream consumers treated as record "
                 "separators and mis-parsed in a way that was hard to diagnose.")
    # Test code only: "which test did this commit touch" is not a question about the corpus.
    suite = repo / "tests"
    suite.mkdir()
    (suite / "test_app.py").write_text(
        "def check_rounding():\n    assert True\n\n\ndef check_padding():\n    assert True\n",
        encoding="utf-8")
    commit(repo, "Add coverage for the split\n\nA regression check so the earlier parsing "
                 "change cannot silently come back when this module is refactored again later.")
    (suite / "test_app.py").write_text(
        "def check_rounding():\n    assert 1 == 1\n\n\ndef check_padding():\n    assert True\n",
        encoding="utf-8")
    commit(repo, "Tighten the coverage assertion\n\nThe check passed trivially and would not "
                 "have caught a regression in the behaviour it claims to be guarding here.")
    return repo, pinned


class Mining(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo, self.pinned = make_repo(self.directory.name)
        self.questions, self.refused, self.considered, self.mixed = commit_suite.build(
            self.repo, self.pinned, "cm", 0, 0, 4, 120, 1200)

    def test_it_mines_the_describable_change(self):
        self.assertEqual(len(self.questions), 1)
        task = self.questions[0]
        self.assertEqual(task["expected_json"]["answer"], "app.py::parse_header")

    def test_a_whitespace_only_commit_yields_nothing(self):
        """`-w` is what makes a reindentation stop producing questions."""
        self.assertIn("no modified definition survives at the pinned revision", self.refused)

    def test_a_message_that_spells_its_target_is_refused(self):
        self.assertEqual(self.refused["message spells a target identifier"], 1)

    def test_no_question_leaks_its_own_answer(self):
        for task in self.questions:
            for identity in [task["expected_json"]["answer"]]:
                self.assertNotIn(identity.split("::")[-1], task["question"])

    def test_the_wording_after_the_message_is_one_constant(self):
        self.assertTrue(self.questions[0]["question"].endswith(commit_suite.ONE))

    def test_the_upstream_prose_survives_and_the_trailers_do_not(self):
        question = self.questions[0]["question"]
        self.assertIn("the first separator now bounds the split", question)
        self.assertNotIn("Signed-off-by", question)

    def test_a_commit_touching_only_test_code_is_refused(self):
        """A suite of test functions would measure retrieval against a part of the tree no user
        asks about, and the first self-mined sample produced exactly that."""
        self.assertEqual(self.refused["only test code changed"], 1)

    def test_test_code_is_recognised_by_path_by_name_and_by_scope(self):
        self.assertTrue(commit_suite.TEST_PATH.search("tests/test_app.py"))
        self.assertTrue(commit_suite.TEST_NAME.match("test_parses"))
        self.assertTrue(commit_suite.TEST_FILE.search("app_test.go"))
        self.assertFalse(commit_suite.TEST_PATH.search("src/latest/mod.rs"))

    def test_including_tests_is_possible_but_off_by_default(self):
        with_tests, _, _, _ = commit_suite.build(
            self.repo, self.pinned, "cm", 0, 0, 4, 120, 1200, include_tests=True)
        self.assertGreater(len(with_tests), len(self.questions))

    def test_the_mining_order_flips_with_which_end_the_snapshot_is(self):
        """Nearest the pinned revision drifts least, and which end that is depends on the
        direction: the oldest commit when the corpus predates the range, the newest when the
        corpus is its HEAD. Mining from the wrong end spends the budget on refusals."""
        oldest = commit_suite.commits_after(self.repo, self.pinned, 0)
        newest = commit_suite.commits_after(self.repo, self.pinned, 0, newest_first=True)
        self.assertEqual(oldest, list(reversed(newest)))
        self.assertEqual(commit_suite.commits_after(self.repo, self.pinned, 1)[0], oldest[0])
        self.assertEqual(
            commit_suite.commits_after(self.repo, self.pinned, 1, newest_first=True)[0],
            newest[0])

    def test_covariates_are_recorded(self):
        covariates = self.questions[0]["covariates"]
        self.assertEqual(covariates["gold_cardinality"], 1)
        self.assertIn("names_target", covariates)

    def test_the_evidence_anchor_exists_in_the_corpus(self):
        anchor = self.questions[0]["evidence"][0]
        source = (self.repo / anchor["path"]).read_text(encoding="utf-8")
        self.assertIn(anchor["contains"], source)

    def test_rejected_alternates_are_other_definitions_of_the_same_file(self):
        alternates = self.questions[0]["rejected_alternates"]
        self.assertTrue(alternates)
        self.assertNotIn("app.py::parse_header", alternates)

    def test_the_compiled_suite_passes_validate_suite(self):
        """The strongest check available offline: what comes out is a suite that compiles."""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "suite.json"
            path.write_text(json.dumps(self.questions), encoding="utf-8")
            result = validate_suite.validate(path, self.repo)
        self.assertEqual(result["problems"], 0, json.dumps(result["findings"], indent=1))


class Maintenance(unittest.TestCase):
    """A commit about tooling describes no repository behaviour, so nothing implements it.

    One reached a smoke - "Bump com.google.errorprone:error_prone_core from 2.20.0 to 2.21.1",
    keyed to `TypeAdapters.java::read` - and all four arms answered differently and wrongly.
    """

    def matches(self, subject):
        return bool(commit_suite.MAINTENANCE.search(subject))

    def test_the_classes_that_describe_no_behaviour(self):
        for subject in ("Bump com.google.errorprone:error_prone_core from 2.20.0 to 2.21.1",
                        "chore: tidy imports", "Suppress serialization warnings.",
                        "Update sources to satisfy a new Error Prone check.",
                        "Migrate all tests to Truth & use `assertThrows`",
                        "Add build config for Gson subset"):
            self.assertTrue(self.matches(subject), subject)

    def test_a_behavioural_subject_survives(self):
        for subject in ("Fix duplicate key detection when first value is null",
                        "Reject non-ASCII digits in the integer parser",
                        "Throw JsonSyntaxException instead of NPE for a null element",
                        "Use LinkedHashMap as the default Map."):
            self.assertFalse(self.matches(subject), subject)

    def test_it_reads_the_subject_not_the_body(self):
        """Matching the body cut 131 of 395 commits: a body mentioning a README is not a
        maintenance commit."""
        body = "Fix a real bug\n\nThe README describes the old behaviour; javadoc updated too."
        self.assertFalse(self.matches(body.splitlines()[0]))
        self.assertTrue(commit_suite.MAINTENANCE.search(body))


class PathLeak(unittest.TestCase):
    def test_naming_the_gold_s_own_file_is_a_leak(self):
        """A subject naming RuntimeTypeAdapterFactory leaves only "which method", and arms
        answered it with zero retrieval calls."""
        self.assertTrue(commit_suite.leaks(
            "Fix `RuntimeTypeAdapterFactory` depending on internal `Streams` class",
            ["extras/src/main/java/com/google/gson/typeadapters/RuntimeTypeAdapterFactory.java"
             "::create"]))

    def test_an_unrelated_file_name_is_not_a_leak(self):
        self.assertFalse(commit_suite.leaks(
            "Reject unpaired surrogates in strict mode",
            ["gson/src/main/java/com/google/gson/stream/JsonReader.java::nextQuotedValue"]))


class Until(unittest.TestCase):
    def test_the_range_end_is_selectable_without_checking_it_out(self):
        """Mining forward from an older pin must not require moving the corpus checkout."""
        with tempfile.TemporaryDirectory() as raw:
            repo, pinned = make_repo(raw)
            head = run(repo, "rev-parse", "HEAD").strip()
            everything = commit_suite.commits_after(repo, pinned, 0)
            self.assertEqual(commit_suite.commits_after(repo, pinned, 0, until=head), everything)
            first = everything[0]
            self.assertEqual(commit_suite.commits_after(repo, pinned, 0, until=first), [first])


class MessageCleaning(unittest.TestCase):
    def test_trailers_and_urls_go(self):
        cleaned = commit_suite.clean_message(
            "Subject line\n\nBody that explains.\nSee https://example.com/x for more.\n"
            "Signed-off-by: Someone <s@example.com>\nFixes: #12\nCo-authored-by: Other")
        self.assertIn("Body that explains.", cleaned)
        self.assertNotIn("Signed-off-by", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("#12", cleaned)


class Covariates(unittest.TestCase):
    def test_a_shared_word_piece_is_recorded_not_cut(self):
        """16 of 21 kernel questions carry one; deleting them would bias the sample."""
        self.assertTrue(commit_suite.names_target(
            "the header parsing path was wrong", ["app.py::parse_header"]))
        self.assertFalse(commit_suite.names_target(
            "the timeout was too short", ["app.py::parse_header"]))

    def test_word_pieces_split_the_usual_spellings(self):
        self.assertEqual(commit_suite.word_pieces("parseHTTPHeader"),
                         {"parse", "http", "header"})
        self.assertEqual(commit_suite.word_pieces("flush_sigqueue"), {"flush", "sigqueue"})

    def test_a_leak_is_word_bounded(self):
        self.assertTrue(commit_suite.leaks("fix parse_header now", ["a.py::parse_header"]))
        self.assertFalse(commit_suite.leaks("fix parse_headers now", ["a.py::parse_header"]))


if __name__ == "__main__":
    unittest.main()
