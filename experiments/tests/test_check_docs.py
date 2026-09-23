import os
import re
import tempfile
import unittest
from pathlib import Path

from check_docs import (INSTALL_BUDGET, README_SECTIONS, check_counts, check_links, check_paths,
                        check_published, check_quickstart, check_shape, check_surface, headings,
                        rust_constants)

CONFIG = '''
pub const TOOLS: [&str; 3] = [
    "search_exact",
    "read_source",
    "search_concept",
];
pub const DEFAULT_SURFACE: [&str; 2] = [
    "search_exact",
    "read_source",
];
pub const PROFILES: [(&str, &[&str]); 2] = [
    ("A", &["search_exact"]),
    ("D", &TOOLS),
];
'''
PROFILE_TABLE = ("| Profile | Equivalent `--tools` |\n"
                 "|---|---|\n"
                 "| A | `search_exact` |\n"
                 "| D | all seven |\n")


class RustConstantsTests(unittest.TestCase):
    def test_parses_catalogue_default_and_profiles_including_the_tools_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "src/config").mkdir(parents=True)
            (repo / "src/config/mod.rs").write_text(CONFIG)

            tools, default, profiles = rust_constants(repo)

            self.assertEqual(tools, ["search_exact", "read_source", "search_concept"])
            self.assertEqual(default, ["search_exact", "read_source"])
            self.assertEqual(profiles["A"], ["search_exact"])
            self.assertEqual(profiles["D"], tools, "`&TOOLS` must expand to the catalogue")


class SurfaceTests(unittest.TestCase):
    def surface(self, text):
        problems = []
        check_surface("doc", text, ["search_exact", "read_source", "search_concept"],
                      ["search_exact", "read_source"],
                      {"A": ["search_exact"], "D": ["search_exact", "read_source",
                                                    "search_concept"]}, problems)
        return [problem["detail"] for problem in problems]

    def test_a_default_surface_that_matches_the_code_passes(self):
        self.assertEqual(
            self.surface("It defaults to the four that repaid their cost: "
                         "`search_exact`, `read_source`."), [])

    def test_a_stale_default_surface_is_reported(self):
        self.assertIn("!= DEFAULT_SURFACE",
                      " ".join(self.surface("It defaults to the four that repaid their cost: "
                                            "`search_exact`, `read_source`, `find_symbol`.")))

    def test_a_stale_profile_row_is_reported(self):
        self.assertIn("profile A documented as", " ".join(self.surface(
            PROFILE_TABLE.replace("| A | `search_exact` |", "| A | `read_source` |"))))

    def test_a_results_table_keyed_by_letter_is_not_read_as_the_profile_table(self):
        self.assertEqual(self.surface("| Profile | Calls |\n|---|---|\n| A | 172 |\n| D | 100 |\n"),
                         [], "only the table mapping profiles to `--tools` is a profile table")

    def test_a_renamed_tool_is_reported_unless_the_rename_is_stated(self):
        self.assertIn("is not a tool", " ".join(self.surface("Adoption of `search_semantic` rose.")))
        self.assertEqual(
            self.surface("`search_semantic` 7 -> 14 - the tool was renamed after these runs."), [])


class CountsTests(unittest.TestCase):
    def counts(self, text):
        problems = []
        check_counts("doc", text, {"library": (15, 0), "stdio": (6, 1), "example": (6, 0)}, 133,
                     problems)
        return [problem["detail"] for problem in problems]

    def test_matching_counts_pass_and_stale_counts_are_reported(self):
        self.assertEqual(self.counts("# 15 library, 6 stdio (1 ignored), 6 example tests"), [])
        self.assertIn("!= actual",
                      " ".join(self.counts("# 15 library, 4 stdio (1 ignored), 6 example tests")))

    def test_a_stale_python_count_is_reported_in_either_phrasing(self):
        self.assertEqual(self.counts("-p 'test_*.py'   # 133 tests"), [])
        self.assertIn("95 != actual 133", " ".join(self.counts("-p 'test_*.py'   # 95 tests")))
        self.assertIn("!= actual", " ".join(self.counts("(133 Python tests, 19 Rust tests)")))
        self.assertEqual(self.counts("(133 Python tests, 27 Rust tests)"), [])


class PathAndLinkTests(unittest.TestCase):
    def test_a_missing_repository_path_is_reported_but_a_foreign_one_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "experiments").mkdir()
            (repo / "experiments/real.py").touch()
            problems = []
            check_paths("doc", "`experiments/real.py` `experiments/gone.py` "
                        "`src/uu/dd/src/conversion_tables.rs`", repo, problems)

            self.assertEqual([problem["detail"] for problem in problems],
                             ["`experiments/gone.py` does not exist"])

    def test_link_targets_and_anchors_are_resolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "other.md").write_text("## Connect Claude Code\n")
            (repo / "doc.md").write_text("")
            problems = []
            check_links("doc.md", "[a](other.md#connect-claude-code) [b](other.md#nope) [c](gone.md)",
                        repo, problems)

            details = [problem["detail"] for problem in problems]
            self.assertEqual(len(details), 2, details)
            self.assertIn("names no heading", details[0])
            self.assertIn("does not exist", details[1])

    def test_heading_slugs_drop_punctuation_the_way_github_does(self):
        self.assertIn("end-to-end-context-efficiency-three-arms-one-corpus",
                      headings("## End-to-end context efficiency: three arms, one corpus"))



class QuickstartTests(unittest.TestCase):
    """The quickstart must be run against the build, not against whatever is installed.

    The check invoked `retrieval-mcp` by name. On a developer machine holding a `cargo install`
    copy that binary answered and the check passed; CI, having none, failed on seven consecutive
    pushes with an empty result. The binary under test is the one in `target/release`.
    """

    def build(self, repo, reply, readme):
        (repo / "target/release").mkdir(parents=True)
        built = repo / "target/release/retrieval-mcp"
        built.write_text(f"#!/bin/sh\necho '{reply}'\n", encoding="utf-8")
        built.chmod(0o755)
        # An installed namesake that answers with nothing, exactly as a stale copy would.
        elsewhere = repo / "bin"
        elsewhere.mkdir()
        installed = elsewhere / "retrieval-mcp"
        installed.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        installed.chmod(0o755)
        (repo / "README.md").write_text(readme, encoding="utf-8")
        problems = []
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{elsewhere}{os.pathsep}{original}"
        try:
            check_quickstart(repo, problems)
        finally:
            os.environ["PATH"] = original
        return problems

    REPLY = ('{"result":{"isError":false,"structuredContent":'
             '{"results":[{"path":"src/main.rs","caller":"main"}],"symbol_status":"indexed"}}}')
    README = ("## Quickstart\n\n```sh\nretrieval-mcp\n```\n\nIt answers:\n\n```json\n"
              '{"results": [{"path": "src/main.rs", "caller": "%s"}], '
              '"symbol_status": "indexed"}\n```\n')

    def test_the_built_binary_answers_even_when_another_is_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.build(Path(directory), self.REPLY, self.README % "main"), [])

    def test_a_sample_response_that_no_longer_matches_the_server_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = self.build(Path(directory), self.REPLY, self.README % "renamed_caller")
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("results[0].caller: documented 'renamed_caller'",
                          problems[0]["detail"])

    def test_a_quickstart_that_shows_no_response_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = self.build(Path(directory), self.REPLY,
                                  "## Quickstart\n\n```sh\nretrieval-mcp\n```\n")
            self.assertEqual([problem["detail"] for problem in problems],
                             ["the quickstart shows no sample response"])


class PublishedNumberTests(unittest.TestCase):
    """A published figure is checked against the run report it came from, not against memory.

    Three releases shipped a table claiming 147 tool calls where its own report said 146, because
    `runs/` is gitignored and nothing compared the two. These are the drifts that must fail.
    """

    EXTRACT = {
        "version": "published-results-v1",
        "declared_percentages": {"-13.3": "instruction bytes, not a run comparison"},
        "studies": {"heldout": {"arms": {
            "native-control": {"correct": 28, "input_tokens": 1149139, "calls": 146,
                               "context_token_turns": 175220, "calls_to_first_evidence": 1.793,
                               "answered_without_evidence": 0},
            "zvec-grep": {"correct": 29, "input_tokens": 1000501, "calls": 78,
                          "context_token_turns": 227059, "calls_to_first_evidence": 1.345,
                          "answered_without_evidence": 0},
            "retrieval-mcp": {"correct": 29, "input_tokens": 763744, "calls": 77,
                              "context_token_turns": 149877, "calls_to_first_evidence": 1.172,
                              "answered_without_evidence": 0}}}},
    }
    TABLE = ("| | native `Read`/`Grep`/`Glob` | zvec-grep 0.2.2 | **retrieval-mcp** |\n"
             "|---|---:|---:|---:|\n"
             "| Correct / 30 | 28 | **29** | **29** |\n"
             "| Input tokens | 1.15 M | 1.00 M | **764 k** |\n"
             "| Tool calls | 146 | 78 | **77** |\n"
             "| Persistent context (tok·turns) | 175 k | 227 k | **150 k** |\n"
             "| Calls to first evidence | 1.79 | 1.35 | **1.17** |\n"
             "| Answered without evidence | **0** | **0** | **0** |\n")

    def published(self, table=None, prose=""):
        text = f"\n## The result\n\n{table if table is not None else self.TABLE}\n{prose}\n## Next\n"
        problems = []
        check_published(text, self.EXTRACT, problems)
        return [problem["detail"] for problem in problems]

    def test_the_table_as_measured_passes_at_the_precision_it_prints(self):
        self.assertEqual(self.published(), [])

    def test_a_rounded_cell_is_accepted_but_a_wrong_one_is_not(self):
        """`764 k` promises 763,744 to the nearest thousand; `750 k` promises something else."""
        self.assertEqual(self.published(self.TABLE.replace("**764 k**", "**763.7 k**")), [])
        self.assertIn("says 750 k but the run report says 763744",
                      " ".join(self.published(self.TABLE.replace("**764 k**", "**750 k**"))))

    def test_the_call_count_drift_that_actually_shipped_is_caught(self):
        stale = self.TABLE.replace("| Tool calls | 146 | 78 | **77** |",
                                   "| Tool calls | 147 | 78 | **78** |")
        details = " ".join(self.published(stale))
        self.assertIn("'Tool calls' for native-control says 147", details)
        self.assertIn("'Tool calls' for retrieval-mcp says 78", details)

    def test_a_percentage_no_study_supports_is_reported(self):
        self.assertEqual(self.published(prose="**−33.5% input tokens**, −47% tool calls.\n"), [])
        self.assertIn("-62% is not a comparison",
                      " ".join(self.published(prose="and −62% tool calls.\n")))

    def test_a_declared_non_run_percentage_is_allowed(self):
        """Instruction bytes are measured somewhere else; the extract says so, so it passes."""
        self.assertEqual(self.published(prose="the routing filter is −13.3% of bytes.\n"), [])

    def test_a_dropped_row_is_reported(self):
        rows = self.TABLE.replace("| Correct / 30 | 28 | **29** | **29** |\n", "")
        self.assertIn("no longer publishes 'Correct'", " ".join(self.published(rows)))

    def test_a_reordered_or_renamed_column_is_refused_rather_than_misread(self):
        swapped = self.TABLE.replace("| | native `Read`/`Grep`/`Glob` | zvec-grep 0.2.2 | "
                                     "**retrieval-mcp** |",
                                     "| | native tools | **retrieval-mcp** | zvec-grep 0.2.2 |")
        self.assertIn("unexpected result table columns", " ".join(self.published(swapped)))


class ShapeTests(unittest.TestCase):
    """The README's order, size and single-source rules, which an audit had to establish twice."""

    def readme(self, sections=None, install="two commands\n", extra=""):
        sections = sections or list(README_SECTIONS)
        contents = " · ".join(
            f"[{name}](#{re.sub(r'[^a-z0-9 -]', '', name.lower()).replace(' ', '-')})"
            for name in README_SECTIONS if name != "Contents")
        body = "# title\n\n"
        for name in sections:
            body += f"## {name}\n\n"
            if name == "Contents":
                body += contents + "\n\n"
            elif name == "Install":
                body += install
                body += "```sh\nclaude mcp add --transport stdio retrieval -- retrieval-mcp\n```\n\n"
            else:
                body += f"prose for {name}\n\n"
        return body + extra

    def shape(self, text):
        problems = []
        check_shape(text, problems)
        return [problem["detail"] for problem in problems]

    def test_the_contract_shape_passes(self):
        self.assertEqual(self.shape(self.readme()), [])

    def test_procedure_before_proof_is_reported(self):
        reordered = list(README_SECTIONS)
        reordered.remove("Install")
        reordered.insert(reordered.index("What it is"), "Install")
        problems = self.shape(self.readme(reordered))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("out of contract order", problems[0])

    def test_an_unlisted_section_and_a_missing_one_are_named(self):
        dropped = [name for name in README_SECTIONS if name != "Troubleshooting"]
        problems = self.shape(self.readme(dropped + ["Frequently asked questions"]))
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("Frequently asked questions", problems[0])
        self.assertIn("Troubleshooting", problems[1])

    def test_an_install_section_that_grew_back_into_reference_material_is_reported(self):
        problems = self.shape(self.readme(install="filler\n" * (INSTALL_BUDGET + 1)))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("over the", problems[0])

    def test_a_second_registration_block_and_a_duplicated_block_are_reported(self):
        repeated = ("```sh\nclaude mcp add --transport stdio retrieval -- retrieval-mcp\n```\n")
        problems = self.shape(self.readme(extra=repeated))
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("appears 2 times", problems[0])
        self.assertIn("2 blocks carry a client registration command", problems[1])

    def test_a_contents_list_that_forgot_a_section_is_reported(self):
        text = self.readme().replace("[Tools](#tools) · ", "")
        problems = self.shape(text)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("does not name every section in order", problems[0])


if __name__ == "__main__":
    unittest.main()
