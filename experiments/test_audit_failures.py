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
