"""Pin study_b's scoring and reporting on a synthetic corpus and a fake arm.

No binary is launched, no index is built, no model is loaded and no network is used: the arm is a
dictionary of rows in the shape every real arm is normalised into, so the scoring path under test
is the one the real study runs.
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality_pass
import study_b

PYTHON = '''class Field:
    # Normalise the raw input and validate it in one step.
    def clean(self, value):
        value = self.to_python(value)
        self.validate(value)
        return value

    def to_python(self, value):
        return value
'''

RUST = '''/// Move a byte offset back to the nearest character boundary.
pub fn floor_boundary(text: &str, offset: usize) -> usize {
    let offset = offset.min(text.len());
    offset
}

pub fn ceil_boundary(text: &str, offset: usize) -> usize {
    offset.min(text.len())
}

pub struct BoundaryError {
    pub offset: usize,
}

impl<'a> BoundaryError {
    pub fn describe(&self) -> String {
        format!("bad offset {}", self.offset)
    }
}
'''

TYPESCRIPT = '''export const DEFAULT_TAB_SIZE = 4;

export class TextModel {
\tpublic getLineContent(lineNumber: number): string {
\t\tconst value = this._buffer.getLineContent(lineNumber);
\t\treturn value;
\t}

\tpublic applyEdits(
\t\toperations: IIdentifiedSingleEditOperation[],
\t\tcomputeUndoEdits: boolean = false
\t): void {
\t\tthis._commandManager.pushEditOperation(operations);
\t}

\tpublic getLineCount(): number {
\t\treturn this._buffer.getLineCount();
\t}
}
'''


def question(identifier, category, answer):
    return {"id": identifier, "category": category, "set": "dev",
            "question": f"question {identifier}", "expected_json": {"answer": answer}}


def row(path, start_line):
    return {"path": path, "start_line": start_line, "end_line": start_line + 2}


def arm(rows, payload=None):
    """A fake arm in the shape mcp_arm and zvec_arm both return."""
    return {"kind": "fake", "rows": rows, "bytes": payload or {}, "latencies": {},
            "cold_ms": None, "index_build_ms": None, "index_build": "none"}


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        (self.root / "pkg").mkdir()
        (self.root / "lib").mkdir()
        (self.root / "editor").mkdir()
        (self.root / "pkg/forms.py").write_text(PYTHON, encoding="utf-8")
        (self.root / "lib/text.rs").write_text(RUST, encoding="utf-8")
        (self.root / "editor/model.ts").write_text(TYPESCRIPT, encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def line(self, relative, text):
        source = (self.root / relative).read_text(encoding="utf-8").splitlines()
        return next(number for number, value in enumerate(source, 1) if value.strip() == text)


class AttributionTests(CorpusTests):
    """One shared attribution function, or the arms are not scored on the same thing."""

    def test_a_row_beginning_on_the_declaration_credits_that_definition(self):
        line = self.line("pkg/forms.py", "def clean(self, value):")
        self.assertEqual(study_b.attribution(self.root / "pkg/forms.py", line), "clean")

    def test_a_row_beginning_on_the_doc_comment_credits_the_definition_below_it(self):
        # v0.1.2 puts the doc comment inside the chunk; reading backwards from it would credit the
        # previous definition, which is how a chunk improvement would look like a ranking loss.
        line = self.line("pkg/forms.py", "# Normalise the raw input and validate it in one step.")
        self.assertEqual(study_b.attribution(self.root / "pkg/forms.py", line), "clean")
        line = self.line("lib/text.rs", "/// Move a byte offset back to the nearest character boundary.")
        self.assertEqual(study_b.attribution(self.root / "lib/text.rs", line), "floor_boundary")

    def test_a_row_inside_a_python_body_credits_the_enclosing_definition(self):
        line = self.line("pkg/forms.py", "self.validate(value)")
        self.assertEqual(study_b.attribution(self.root / "pkg/forms.py", line), "clean")

    def test_a_row_inside_a_brace_language_body_is_not_unattributed(self):
        # Harness defect 17: audit_failures.enclosing() implemented only the Python branch, so
        # every Rust and TypeScript row resolved to None and no brace-language arm could score.
        line = self.line("lib/text.rs", "let offset = offset.min(text.len());")
        self.assertEqual(study_b.attribution(self.root / "lib/text.rs", line), "floor_boundary")
        line = self.line("editor/model.ts", "const value = this._buffer.getLineContent(lineNumber);")
        self.assertEqual(study_b.attribution(self.root / "editor/model.ts", line), "getLineContent")

    def test_a_wrapped_typescript_signature_credits_its_own_method(self):
        line = self.line("editor/model.ts", "public applyEdits(")
        self.assertEqual(study_b.attribution(self.root / "editor/model.ts", line), "applyEdits")

    def test_a_module_level_binding_is_still_a_definition(self):
        line = self.line("editor/model.ts", "export const DEFAULT_TAB_SIZE = 4;")
        self.assertEqual(study_b.attribution(self.root / "editor/model.ts", line),
                         "DEFAULT_TAB_SIZE")

    def test_a_rust_type_declaration_is_a_definition(self):
        # A gold may name a struct, enum or trait. Attributing those rows to nothing would lose
        # real hits for every arm alike, which looks like a hard question rather than a hole.
        line = self.line("lib/text.rs", "pub struct BoundaryError {")
        self.assertEqual(study_b.attribution(self.root / "lib/text.rs", line), "BoundaryError")

    def test_a_rust_impl_block_names_the_type_it_belongs_to(self):
        # zvec-grep's chunker returns whole `impl` blocks. Leaving them unattributed would score
        # an arm's chunk granularity rather than its ranking.
        line = self.line("lib/text.rs", "impl<'a> BoundaryError {")
        self.assertEqual(study_b.attribution(self.root / "lib/text.rs", line), "BoundaryError")

    def test_a_method_inside_an_impl_block_beats_the_impl(self):
        line = self.line("lib/text.rs", "pub fn describe(&self) -> String {")
        self.assertEqual(study_b.attribution(self.root / "lib/text.rs", line), "describe")
        line = self.line("lib/text.rs", 'format!("bad offset {}", self.offset)')
        self.assertEqual(study_b.attribution(self.root / "lib/text.rs", line), "describe")


class ScoringTests(CorpusTests):
    def test_the_right_file_with_the_wrong_definition_does_not_score(self):
        task = question("q1", "vague_conceptual", "pkg/forms.py::clean")
        outcome = arm({"q1": [row("pkg/forms.py", self.line("pkg/forms.py",
                                                            "def to_python(self, value):"))]})
        condition = study_b.evaluate(self.root, [task], outcome)
        self.assertEqual(condition["cells"]["q1"]["ranked"], ["pkg/forms.py::to_python"])
        self.assertIsNone(condition["cells"]["q1"]["rank"])
        self.assertEqual(condition["effectiveness"]["recall@10"], 0)

    def test_the_same_file_at_the_gold_definition_does_score(self):
        task = question("q1", "vague_conceptual", "pkg/forms.py::clean")
        outcome = arm({"q1": [row("pkg/forms.py", self.line("pkg/forms.py",
                                                            "def clean(self, value):"))]})
        condition = study_b.evaluate(self.root, [task], outcome)
        self.assertEqual(condition["cells"]["q1"]["rank"], 1)

    def test_ranks_map_to_the_expected_mrr(self):
        clean = self.line("pkg/forms.py", "def clean(self, value):")
        other = self.line("pkg/forms.py", "def to_python(self, value):")
        floor = self.line("lib/text.rs", "pub fn floor_boundary(text: &str, offset: usize) -> usize {")
        tasks = [question("first", "vague_conceptual", "pkg/forms.py::clean"),
                 question("second", "vague_conceptual", "lib/text.rs::floor_boundary"),
                 question("third", "exact_control", "editor/model.ts::getLineCount")]
        outcome = arm({
            "first": [row("pkg/forms.py", clean), row("pkg/forms.py", other)],
            "second": [row("pkg/forms.py", other), row("lib/text.rs", floor)],
            "third": [row("pkg/forms.py", other)],
        })
        condition = study_b.evaluate(self.root, tasks, outcome)
        ranks = {name: cell["rank"] for name, cell in condition["cells"].items()}
        self.assertEqual(ranks, {"first": 1, "second": 2, "third": None})
        effectiveness = condition["effectiveness"]
        self.assertEqual(effectiveness["mrr"], round((1 + 0.5 + 0) / 3, 4))
        self.assertEqual(effectiveness["recall@1"], 1)
        self.assertEqual(effectiveness["recall@3"], 2)

    def test_bucket_rows_sum_to_the_overall_row(self):
        clean = self.line("pkg/forms.py", "def clean(self, value):")
        other = self.line("pkg/forms.py", "def to_python(self, value):")
        floor = self.line("lib/text.rs", "pub fn floor_boundary(text: &str, offset: usize) -> usize {")
        tasks = [question("first", "vague_conceptual", "pkg/forms.py::clean"),
                 question("second", "vague_conceptual", "lib/text.rs::floor_boundary"),
                 question("third", "exact_control", "editor/model.ts::getLineCount"),
                 question("fourth", "generic_name", "pkg/forms.py::to_python")]
        outcome = arm({
            "first": [row("pkg/forms.py", clean)],
            "second": [row("pkg/forms.py", other), row("lib/text.rs", floor)],
            "third": [],
            "fourth": [row("pkg/forms.py", other)],
        })
        effectiveness = study_b.evaluate(self.root, tasks, outcome)["effectiveness"]
        buckets = effectiveness["by_category"]
        self.assertEqual(set(buckets), {"vague_conceptual", "exact_control", "generic_name"})
        self.assertEqual(sum(bucket["n"] for bucket in buckets.values()), effectiveness["n"])
        for cutoff in study_b.CUTOFFS:
            self.assertEqual(sum(bucket[f"recall@{cutoff}"] for bucket in buckets.values()),
                             effectiveness[f"recall@{cutoff}"],
                             f"bucket recall@{cutoff} must sum to the overall row")

    def test_an_exhaustive_gold_records_every_identity_separately(self):
        clean = self.line("pkg/forms.py", "def clean(self, value):")
        task = question("q1", "direct_caller_lookup",
                        ["pkg/forms.py::clean", "lib/text.rs::ceil_boundary"])
        condition = study_b.evaluate(self.root, [task], arm({"q1": [row("pkg/forms.py", clean)]}))
        self.assertEqual(condition["cells"]["q1"]["gold_ranks"],
                         {"pkg/forms.py::clean": 1, "lib/text.rs::ceil_boundary": None})
        self.assertEqual(condition["cells"]["q1"]["rank"], 1)

    def test_a_row_outside_the_corpus_is_never_a_hit(self):
        task = question("q1", "vague_conceptual", "pkg/forms.py::clean")
        condition = study_b.evaluate(self.root, [task], arm({"q1": [row("../escape/forms.py", 3)]}))
        self.assertIsNone(condition["cells"]["q1"]["rank"])


class GradabilityTests(CorpusTests):
    """A benchmark that silently drops what it cannot grade is worse than one that grades nothing."""

    def test_a_gold_in_this_corpus_is_gradable(self):
        task = question("q1", "vague_conceptual", "pkg/forms.py::clean")
        self.assertIsNone(study_b.gradable(task, self.root))

    def test_a_gold_naming_another_corpus_reports_its_reason(self):
        task = question("q1", "vague_conceptual", "src/uucore/src/lib/other.rs::floor_boundary")
        self.assertIn("gold path not in this corpus", study_b.gradable(task, self.root))

    def test_a_null_gold_is_not_scored_as_a_ranking_question(self):
        task = question("q1", "vague_conceptual", None)
        self.assertIn("no path-qualified symbol", study_b.gradable(task, self.root))

    def test_a_gold_whose_symbol_the_corpus_does_not_define_reports_its_reason(self):
        task = question("q1", "vague_conceptual", "pkg/forms.py::no_such_method")
        self.assertIn("corpus file defines no such symbol", study_b.gradable(task, self.root))

    def test_a_restricted_visibility_rust_definition_is_gradable(self):
        # quality_pass.definitions' Rust pattern is `^\s*(pub\s+)?(async\s+)?fn`, so a
        # `pub(crate) fn` gold is missing from that index. Refusing to grade it would report an
        # authoring error for a symbol every arm can in fact hit, so gradability is decided by the
        # same declaration table attribution uses.
        (self.root / "lib/scoped.rs").write_text(
            "pub(crate) fn shrink(text: &str) -> usize {\n    text.len()\n}\n", encoding="utf-8")
        self.assertNotIn("shrink", quality_pass.definitions(self.root))
        task = question("q1", "generic_name", "lib/scoped.rs::shrink")
        self.assertIsNone(study_b.gradable(task, self.root))


class ArmTests(unittest.TestCase):
    def test_arm_specs_parse_and_bad_ones_are_refused(self):
        self.assertEqual(study_b.parse_arm("mcp:v020=target/release/retrieval-mcp"),
                         {"kind": "mcp", "name": "v020", "binary": Path("target/release/retrieval-mcp")})
        self.assertEqual(study_b.parse_arm("zvec:zg022=/opt/zg")["kind"], "zvec")
        for bad in ("mcp:v020", "grep:x=/bin/rg", "mcp:=/bin/rg", "nonsense"):
            with self.assertRaises(Exception, msg=bad):
                study_b.parse_arm(bad)

    def test_zvec_agent_markdown_parses_into_the_shared_row_shape(self):
        # zvec-grep 0.2.2 has no JSON output mode; this is the format `zg query` actually prints.
        text = ("query groups (1):\n"
                "Q1 [primary]: where the form field cleans its value\n"
                "hits: 2\n\n"
                "#1 matchedBy=fts+vector pkg/forms.py:2493-2503\n"
                "2493\tdef formfield(self, **kwargs):\n\n"
                "#2 matchedBy=vector lib/text.rs:15-16\n"
                "symbol: function floor_boundary scope: text\n")
        self.assertEqual(study_b.zvec_rows(text),
                         [{"path": "pkg/forms.py", "start_line": 2493, "end_line": 2503},
                          {"path": "lib/text.rs", "start_line": 15, "end_line": 16}])

    def test_zvec_state_is_forced_outside_the_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            environment = study_b.zvec_environment(state, Path(directory) / "models")
        self.assertEqual(environment["ZVEC_GREP_HOME"], str(state / "zvec-home"))
        self.assertEqual(environment["ZVEC_GREP_MODE"], "direct")


class ReportTests(CorpusTests):
    def test_the_rendered_report_names_every_skipped_question_and_every_bucket(self):
        clean = self.line("pkg/forms.py", "def clean(self, value):")
        tasks = [question("first", "vague_conceptual", "pkg/forms.py::clean"),
                 question("second", "exact_control", "elsewhere/other.py::missing")]
        graded = [task for task in tasks if not study_b.gradable(task, self.root)]
        outcome = arm({"first": [row("pkg/forms.py", clean)]}, {"first": 512, "second": 64})
        result = {
            "version": "study-b-v1",
            "corpus": {"sha256": "0" * 64, "files": 3},
            "corpus_root": str(self.root),
            "questions": len(tasks),
            "questions_graded": [task["id"] for task in graded],
            "questions_skipped": [{"id": task["id"], "reason": study_b.gradable(task, self.root)}
                                  for task in tasks if study_b.gradable(task, self.root)],
            "limit": 10,
            "conditions": {"fake": {"kind": "fake", "binary": "none",
                                    **study_b.evaluate(self.root, graded, outcome)}},
            "ranks": {"first": {"fake": 1}},
            "limitations": "",
        }
        text = study_b.render(result)
        self.assertIn("graded 1/2 questions", text)
        self.assertIn("skip second", text)
        self.assertIn("vague_conceptual", text)
        self.assertIn("bytes/query 288", text)


if __name__ == "__main__":
    unittest.main()
