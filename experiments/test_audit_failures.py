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


GO = '''package service

type Table struct{ Rows []string }

func (t *Table) Render() []string {
\tout := []string{}
\tfor _, row := range t.Rows {
\t\tout = append(out, normalize(row))
\t}
\treturn &Table{Rows: out}.Rows
}

func (t Table) Label() string { return normalize("label") }

func Report(rows []string) {
\tgo func() { _ = normalize("async") }()
}
'''

JAVA = '''package com.example;

public class Report {
  private final String title = normalize("t");

  @Override
  public String render(String row) {
    return normalize(row);
  }

  public java.util.List<String> lines(
      java.util.List<String> rows) {
    return java.util.List.of(normalize("x"));
  }
}
'''

CPP = '''#include "engine.h"

namespace report {

TableCache::TableCache(const std::string& name,
                       int entries)
    : name_(name),
      cache_(normalize(entries)) {}

int Engine::render(int row) const { return normalize(row); }

void drive() {
  TEST("a macro block is not a definition") {
    normalize(1);
  }
}

}  // namespace report
'''

JAVASCRIPT = '''const { normalize } = require("./util");

module.exports = {
    create(context) {
        return {
            ReturnStatement(node) {
                context.report({
                    node,
                    fix(fixer) {
                        return normalize(fixer);
                    }
                });
            }
        };
    }
};
'''


class LanguageFamilyAttributionTests(unittest.TestCase):
    """Attribution in the families added after Rust, Python and TypeScript.

    Each case here was a wrong answer first, found by `language_audit.py` comparing these rows
    with the server's on Cobra, Gson, LevelDB, Redis and ESLint. Every one of them would have
    made a correct caller gold unverifiable, which is how a real capability gets recorded as a
    failure.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.files = {}
        for name, text in (("service.go", GO), ("Report.java", JAVA), ("engine.cc", CPP),
                           ("rule.js", JAVASCRIPT)):
            path = self.root / name
            path.write_text(text, encoding="utf-8")
            self.files[name] = (path, {line.strip(): number for number, line
                                       in enumerate(text.splitlines(), 1)})

    def tearDown(self):
        self.directory.cleanup()

    def at(self, name, text):
        path, lines = self.files[name]
        return audit_failures.enclosing(path, lines[text])

    def test_a_go_method_owns_its_body_and_a_struct_literal_owns_nothing(self):
        self.assertEqual(self.at("service.go", "out = append(out, normalize(row))"), "Render")
        self.assertEqual(self.at("service.go", "return &Table{Rows: out}.Rows"), "Render")

    def test_a_go_one_line_method_is_the_caller_of_the_call_on_its_own_line(self):
        self.assertEqual(self.at("service.go", 'func (t Table) Label() string { return normalize("label") }'),
                         "Label")

    def test_a_goroutine_literal_belongs_to_the_function_that_launched_it(self):
        self.assertEqual(self.at("service.go", 'go func() { _ = normalize("async") }()'), "Report")

    def test_an_annotated_java_method_owns_its_body(self):
        self.assertEqual(self.at("Report.java", "return normalize(row);"), "render")

    def test_a_java_field_initialiser_belongs_to_its_class(self):
        self.assertEqual(self.at("Report.java", 'private final String title = normalize("t");'),
                         "Report")

    def test_a_wrapped_java_signature_still_owns_its_body(self):
        self.assertEqual(self.at("Report.java", 'return java.util.List.of(normalize("x"));'),
                         "lines")

    def test_a_cpp_initialiser_list_names_the_constructor_not_the_member(self):
        self.assertEqual(self.at("engine.cc", ": name_(name),"), "TableCache")
        self.assertEqual(self.at("engine.cc", "cache_(normalize(entries)) {}"), "TableCache")

    def test_a_cpp_one_line_method_owns_the_call_beside_it(self):
        self.assertEqual(self.at("engine.cc", "int Engine::render(int row) const { return normalize(row); }"),
                         "render")

    def test_a_macro_block_inside_a_function_is_not_a_definition(self):
        self.assertEqual(self.at("engine.cc", "normalize(1);"), "drive")

    def test_a_shorthand_method_in_an_object_literal_is_the_caller(self):
        self.assertEqual(self.at("rule.js", "return normalize(fixer);"), "fix")

    def test_a_go_interface_method_signature_is_not_a_call(self):
        """Nothing calls anything from inside a type.

        `MetricsRecorder() stats.MetricsRecorder` in a gRPC-Go `interface` body declares a
        method. Counted as a call it demanded that an exhaustive gold name `ClientConn` as a
        caller of its own member - a caller no system under test reports, on the first Go suite
        this repository ever compiled.
        """
        (self.root / "iface.go").write_text(
            "package service\n\n"
            "type Recorder interface {\n"
            "\tnormalize(row string) string\n"
            "}\n\n"
            "func drive(r Recorder) string { return normalize(\"x\") }\n",
            encoding="utf-8")
        (self.root / "util.go").write_text(
            "package service\n\nfunc normalize(v string) string { return v }\n",
            encoding="utf-8")
        callers = audit_failures.true_callers(self.root, "normalize", "util.go")
        self.assertNotIn("iface.go::Recorder", callers)
        self.assertIn("iface.go::drive", callers)

    def test_callers_are_enumerated_in_every_family(self):
        (self.root / "util.js").write_text("function normalize(v) { return v; }\n",
                                           encoding="utf-8")
        callers = audit_failures.true_callers(self.root, "normalize", "util.js")
        self.assertEqual(callers, [
            "Report.java::Report", "Report.java::lines", "Report.java::render",
            "engine.cc::TableCache", "engine.cc::drive", "engine.cc::render",
            "rule.js::fix",
            "service.go::Label", "service.go::Render", "service.go::Report",
        ])

    def test_a_qualified_static_call_is_not_read_as_a_prototype(self):
        (self.root / "batch.h").write_text(
            "class WriteBatchInternal {\n public:\n  static void SetSequence(int seq);\n};\n",
            encoding="utf-8")
        (self.root / "db.cc").write_text(
            "#include \"batch.h\"\n"
            "void Write(int seq) {\n  WriteBatchInternal::SetSequence(seq);\n}\n",
            encoding="utf-8")
        callers = audit_failures.true_callers(self.root, "SetSequence", "batch.cc")
        self.assertEqual(callers, ["db.cc::Write"])



class ScopeAndLiteralTests(unittest.TestCase):
    """The two rules that let a caller question be asked the way a user asks it."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_a_name_inside_a_format_string_is_not_a_call(self):
        (self.root / "server.go").write_text(
            "package p\n\n"
            "func RegisterService(d *D) {}\n\n"
            "func register(d *D) {\n"
            "\tlogf(\"RegisterService(%q)\", d.Name)\n"
            "}\n", encoding="utf-8")
        self.assertEqual(
            audit_failures.true_callers(self.root, "RegisterService", "server.go",
                                        include_defining_file=True), [])

    def test_the_defining_file_is_excluded_by_default_and_included_on_request(self):
        (self.root / "util.go").write_text(
            "package p\n\n"
            "func normalize(s string) string { return s }\n\n"
            "func local(s string) string { return normalize(s) }\n", encoding="utf-8")
        (self.root / "other.go").write_text(
            "package p\n\nfunc remote(s string) string { return normalize(s) }\n", encoding="utf-8")
        self.assertEqual(audit_failures.true_callers(self.root, "normalize", "util.go"),
                         ["other.go::remote"])
        self.assertEqual(
            audit_failures.true_callers(self.root, "normalize", "util.go",
                                        include_defining_file=True),
            ["other.go::remote", "util.go::local"])



class GoKeywordNameTests(unittest.TestCase):
    """`func (tw *storeTxnWrite) delete(...)` is a method whose name is a JavaScript operator."""

    def test_a_go_method_named_after_a_control_word_still_owns_its_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "store.go").write_text(
                "package p\n\n"
                "func newKey(rev int64) string { return \"\" }\n\n"
                "func (t *txn) delete(key []byte) {\n"
                "\t_ = newKey(1)\n"
                "}\n", encoding="utf-8")
            self.assertEqual(
                audit_failures.true_callers(root, "newKey", "other.go"),
                ["store.go::delete"])


if __name__ == "__main__":
    unittest.main()
