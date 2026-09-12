import tempfile
import unittest
from pathlib import Path

from check_docs import (check_counts, check_links, check_paths, check_surface, headings,
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


if __name__ == "__main__":
    unittest.main()
