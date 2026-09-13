import tempfile
import unittest
from pathlib import Path

import audit_failures

SOURCE = '''class Command:
    def write_migration_files(self, changes):
        run_formatters(self.written_files)

    def handle_merge(self, loader, conflicts):
        if self.interactive:
            def all_items_equal(seq):
                return all(item == seq[0] for item in seq)

            run_formatters([writer.path])

    @cached_property
    def _constraint_names(
        self,
        table,
    ):
        return truncate_name(table)

    def documented(self):
        # these are Field.run_formatters() methods
        return None
'''


class EnclosingDefinitionTests(unittest.TestCase):
    """Gold verification attributes call sites; every bug here demands a wrong exhaustive gold."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "makemigrations.py"
        self.path.write_text(SOURCE, encoding="utf-8")
        self.lines = {text.strip(): number
                      for number, text in enumerate(SOURCE.splitlines(), 1)}

    def tearDown(self):
        self.directory.cleanup()

    def test_a_closed_nested_helper_does_not_capture_a_later_call(self):
        line = self.lines["run_formatters([writer.path])"]
        self.assertEqual(audit_failures.enclosing(self.path, line), "handle_merge")

    def test_a_decorated_wrapped_signature_does_not_credit_the_class(self):
        line = self.lines["return truncate_name(table)"]
        self.assertEqual(audit_failures.enclosing(self.path, line), "_constraint_names")

    def test_a_plain_call_still_resolves_to_its_method(self):
        line = self.lines["run_formatters(self.written_files)"]
        self.assertEqual(audit_failures.enclosing(self.path, line), "write_migration_files")


TYPESCRIPT = '''/*---------------------------------------------------------------------------------------------
 *  A licence banner whose braces { } are prose, not scope.
 *--------------------------------------------------------------------------------------------*/

export class ViewModel extends Disposable {

\tprivate readonly _opener = { open: () => this._reveal() };

\tpublic reveal(target: Range): void {
\t\tthis._register(addDisposableListener(this._node, 'click', () => {
\t\t\tlookupSelection(target);
\t\t}));
\t}

\tpublic describe(): { label: string } {
\t\treturn { label: lookupSelection(this._node) };
\t}

\tpublic compare(
\t\tleft: Range,
\t\tright: Range,
\t): boolean {
\t\treturn lookupSelection(left) === lookupSelection(right);
\t}

\tpublic documented(): void {
\t\t// lookupSelection() named here is documentation
\t\tconst brace = '{';
\t\treturn undefined;
\t}
}

export function outer(): void {
\tlookupSelection(undefined);
\tconst observer = new ResizeObserver(() => {
\t\tlookupSelection(observer);
\t});
}
'''

RUST = '''use crate::format;

impl<'a> Settings<'a> {
    fn new(matches: &'a ArgMatches) -> Self {
        let brace = '{';
        parse_width(matches)
    }

    pub fn render<T>(&self, value: T) -> String
    where
        T: Display,
    {
        let raw = r#"a { b"#;
        parse_width(value)
    }
}

fn outer(path: &Path) -> Result<()> {
    if let Ok(v) = try_thing(path) {
        parse_width(v);
    }
    match size {
        Some(n) => {
            parse_width(n);
        }
        Err(error) => {
            let raise = |code: i32| {
                parse_width(code);
            };
            raise(1);
        }
    }
    Ok(())
}
'''


class BraceLanguageEnclosingTests(unittest.TestCase):
    """Attribution in `{}` languages. Every bug here empties a caller set that is really populated.

    Before this branch existed, `enclosing` fell through to `None` for every non-Python suffix, so
    `true_callers` returned the empty set on any Rust or TypeScript corpus and every exhaustive
    caller gold over one failed verification no matter how correct it was.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.typescript = root / "viewModel.ts"
        self.typescript.write_text(TYPESCRIPT, encoding="utf-8")
        self.rust = root / "wc.rs"
        self.rust.write_text(RUST, encoding="utf-8")
        self.lines = {
            self.typescript: {text.strip(): number for number, text
                              in enumerate(TYPESCRIPT.splitlines(), 1)},
            self.rust: {text.strip(): number for number, text
                        in enumerate(RUST.splitlines(), 1)},
        }

    def tearDown(self):
        self.directory.cleanup()

    def at(self, path, text):
        return audit_failures.enclosing(path, self.lines[path][text])

    def test_a_call_in_a_closure_belongs_to_the_method_containing_it(self):
        self.assertEqual(self.at(self.typescript, "lookupSelection(target);"), "reveal")

    def test_a_type_literal_return_annotation_does_not_swallow_the_body(self):
        self.assertEqual(self.at(self.typescript, "return { label: lookupSelection(this._node) };"),
                         "describe")

    def test_a_wrapped_signature_still_owns_its_body(self):
        self.assertEqual(
            self.at(self.typescript, "return lookupSelection(left) === lookupSelection(right);"),
            "compare")

    def test_a_brace_in_a_banner_comment_or_a_string_opens_no_scope(self):
        self.assertEqual(self.at(self.typescript, "return undefined;"), "documented")

    def test_a_top_level_function_is_not_credited_to_the_class_above_it(self):
        self.assertEqual(self.at(self.typescript, "lookupSelection(undefined);"), "outer")

    def test_a_callback_passed_to_a_constructor_is_not_a_definition(self):
        self.assertEqual(self.at(self.typescript, "lookupSelection(observer);"), "outer")

    def test_rust_credits_the_function_not_the_impl_block(self):
        self.assertEqual(self.at(self.rust, "parse_width(matches)"), "new")

    def test_a_where_clause_and_a_raw_string_do_not_break_rust_scope(self):
        self.assertEqual(self.at(self.rust, "parse_width(value)"), "render")

    def test_an_if_let_guard_is_not_a_caller(self):
        # `if let Ok(v) = try_thing(path) {` reads exactly like a TypeScript method header. Naming
        # its frame credited `Ok` with the call and lost the function that really makes it.
        self.assertEqual(self.at(self.rust, "parse_width(v);"), "outer")

    def test_a_match_arm_is_not_a_caller(self):
        self.assertEqual(self.at(self.rust, "parse_width(n);"), "outer")

    def test_a_rust_closure_binding_is_not_a_caller(self):
        self.assertEqual(self.at(self.rust, "parse_width(code);"), "outer")

    def test_rust_callers_name_functions_only(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory)
            (corpus / "width.rs").write_text(
                "pub fn parse_width(value: usize) -> usize {\n    value\n}\n", encoding="utf-8")
            (corpus / "wc.rs").write_text(RUST, encoding="utf-8")
            callers = audit_failures.true_callers(corpus, "parse_width", "width.rs")
        self.assertEqual(callers, ["wc.rs::new", "wc.rs::outer", "wc.rs::render"])

    def test_an_unparseable_suffix_yields_no_owner_rather_than_raising(self):
        unknown = self.typescript.with_suffix(".md")
        unknown.write_text("# {\n", encoding="utf-8")
        self.assertIsNone(audit_failures.enclosing(unknown, 1))

    def test_callers_are_enumerated_across_a_brace_language_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory)
            (corpus / "lookup.ts").write_text(
                "export function lookupSelection(node: unknown): string {\n\treturn '';\n}\n",
                encoding="utf-8")
            (corpus / "viewModel.ts").write_text(TYPESCRIPT, encoding="utf-8")
            callers = audit_failures.true_callers(corpus, "lookupSelection", "lookup.ts")
        self.assertEqual(callers, ["viewModel.ts::compare", "viewModel.ts::describe",
                                   "viewModel.ts::outer", "viewModel.ts::reveal"])


class CallSiteTests(unittest.TestCase):
    def test_a_call_written_in_a_comment_is_not_a_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory)
            (corpus / "helper.py").write_text("def run_formatters(paths):\n    return paths\n",
                                              encoding="utf-8")
            (corpus / "user.py").write_text(SOURCE, encoding="utf-8")
            callers = audit_failures.true_callers(corpus, "run_formatters", "helper.py")
        self.assertEqual(callers, ["user.py::handle_merge", "user.py::write_migration_files"])


if __name__ == "__main__":
    unittest.main()
