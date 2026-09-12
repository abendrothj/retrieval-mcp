import shutil
import tempfile
import unittest
from pathlib import Path

from quality_pass import (DECLINE, answer_json, credit, definitions, mentions, parse_symbol,
                          resolve)

INDEX = {"break_lines": {"src/uu/fmt/src/linebreak.rs"},
         "render": {"src/uu/dd/src/diagnostics.rs", "src/uu/ls/src/render.rs"},
         "locate_operand": {"src/uucore/src/lib/features/diagnostics.rs"}}


class ResolverTests(unittest.TestCase):
    def test_notation_does_not_matter_when_the_name_is_unique(self):
        for written in ("linebreak::break_lines", "src/uu/fmt/src/linebreak.rs::break_lines", "break_lines"):
            self.assertEqual(resolve(written, INDEX)[0], "src/uu/fmt/src/linebreak.rs", written)

    def test_an_ambiguous_name_needs_the_context_the_answer_supplied(self):
        self.assertIsNone(resolve("render", INDEX), "a name in two files must not resolve on its own")
        self.assertEqual(resolve("uu_dd::diagnostics::render", INDEX)[0], "src/uu/dd/src/diagnostics.rs")

    def test_a_type_qualifier_is_kept(self):
        self.assertEqual(parse_symbol("uucore::diagnostics::Snapshot::locate_operand"),
                         ("Snapshot", "locate_operand"))

    def test_unknown_names_do_not_resolve(self):
        self.assertIsNone(resolve("nonexistent_symbol", INDEX))

    def test_a_dotted_owner_is_the_same_symbol_as_a_bare_leaf(self):
        """`base.py::Model.from_db` is how a Python reader writes it; it is not a wrong answer."""
        index = {"from_db": {"django/db/models/base.py"},
                 "create": {"django/apps/config.py", "django/db/models/query.py"}}
        self.assertEqual(parse_symbol("django/db/models/base.py::Model.from_db"),
                         ("Model", "from_db"))
        gold = "django/db/models/base.py::from_db"
        self.assertEqual(credit("django/db/models/base.py::Model.from_db", gold, index), 1.0)
        self.assertEqual(credit("django.apps.config::AppConfig.create",
                                "django/apps/config.py::create", index), 1.0)
        self.assertEqual(credit("django/db/models/query.py::QuerySet.create", gold, index), 0.0)

    def test_an_editor_style_single_colon_names_the_same_definition(self):
        """`query.py:Query.combine` is how grep and editors write it; a line number is not a name."""
        index = {"combine": {"django/db/models/sql/query.py"}}
        gold = "django/db/models/sql/query.py::combine"
        self.assertEqual(credit("django/db/models/sql/query.py:Query.combine", gold, index), 1.0)
        self.assertIsNone(resolve("django/db/models/sql/query.py:1234", index))


TS_INDEX = {"getResolvedShellEnv": {"src/vs/platform/shell/node/shellEnv.ts"},
            "isSuccess": {"src/vs/platform/request/common/request.ts",
                          "src/vs/platform/userDataSync/common/userDataSyncStoreService.ts"},
            "hasNoContent": {"src/vs/platform/request/common/request.ts"},
            "statusCode": {"src/vs/platform/request/node/requestService.ts"},
            "request": {"src/vs/platform/request/node/requestService.ts"}}


class ProseSpellingTests(unittest.TestCase):
    """A right answer must not fail because it was written as a sentence."""

    GOLD = "src/vs/platform/shell/node/shellEnv.ts::getResolvedShellEnv"

    def test_a_sentence_naming_one_definition_resolves_like_a_qualified_symbol(self):
        for written in ("getResolvedShellEnv in src/vs/platform/shell/node/shellEnv.ts",
                        "src/vs/platform/shell/node/shellEnv.ts::getResolvedShellEnv",
                        "getResolvedShellEnv"):
            self.assertEqual(credit(written, self.GOLD, TS_INDEX), 1.0, written)

    def test_a_different_symbol_is_still_wrong_however_it_is_spelled(self):
        self.assertEqual(
            credit("isSuccess in src/vs/platform/request/common/request.ts", self.GOLD, TS_INDEX), 0.0)

    def test_listing_candidates_earns_nothing(self):
        self.assertEqual(credit("either getResolvedShellEnv or isSuccess", self.GOLD, TS_INDEX), 0.0)
        self.assertEqual(
            credit("isSuccess or hasNoContent in src/vs/platform/request/common/request.ts",
                   "src/vs/platform/request/common/request.ts::isSuccess", TS_INDEX), 0.0)

    def test_a_namesake_needs_the_file_the_answer_supplied(self):
        gold = "src/vs/platform/request/common/request.ts::isSuccess"
        self.assertEqual(credit("isSuccess", gold, TS_INDEX), 0.0)
        self.assertEqual(
            credit("isSuccess, defined in src/vs/platform/request/common/request.ts near the "
                   "statusCode check", gold, TS_INDEX), 1.0)


class SetAnswerTests(unittest.TestCase):
    """A set-valued answer written as a sentence is still an answer."""

    GOLD = ["src/vs/platform/shell/node/shellEnv.ts::getResolvedShellEnv",
            "src/vs/platform/request/common/request.ts::hasNoContent"]

    def test_prose_naming_every_identity_scores_one(self):
        self.assertEqual(credit("getResolvedShellEnv and hasNoContent", self.GOLD, TS_INDEX), 1.0)

    def test_prose_naming_half_scores_half(self):
        self.assertEqual(credit("only getResolvedShellEnv", self.GOLD, TS_INDEX), 0.5)

    def test_an_object_gold_accepts_prose_naming_both_parts(self):
        gold = {"implementation": "src/vs/platform/request/common/request.ts::hasNoContent",
                "consumer": "src/vs/platform/shell/node/shellEnv.ts::getResolvedShellEnv"}
        self.assertEqual(credit("hasNoContent, consumed by getResolvedShellEnv", gold, TS_INDEX), 1.0)
        self.assertEqual(credit("hasNoContent alone", gold, TS_INDEX), 0.5)

    def test_a_namesake_in_prose_needs_its_file(self):
        gold = ["src/vs/platform/request/common/request.ts::isSuccess"]
        self.assertEqual(credit("isSuccess", gold, TS_INDEX), 0.0)
        self.assertEqual(
            credit("isSuccess in src/vs/platform/request/common/request.ts", gold, TS_INDEX), 1.0)
        self.assertFalse(mentions("isSuccess", gold[0], TS_INDEX))


class PythonIndexTests(unittest.TestCase):
    def test_classes_functions_and_async_functions_are_indexed(self):
        if not shutil.which("rg"):
            self.skipTest("ripgrep is required to build the definition index")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text(
                "class Service:\n"
                "    async def fetch(self):\n"
                "        pass\n\n"
                "def build():\n"
                "    pass\n",
                encoding="utf-8",
            )
            index = definitions(root)
        for name in ("Service", "fetch", "build"):
            self.assertEqual(index[name], {"service.py"})


class TypeScriptIndexTests(unittest.TestCase):
    def test_methods_with_several_modifiers_are_indexed(self):
        """`private async request(` was read as no definition at all, losing every such method."""
        if not shutil.which("rg"):
            self.skipTest("ripgrep is required to build the definition index")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.ts").write_text(
                "export class Store {\n"
                "  private async request(url: string) { return url; }\n"
                "  protected static override handle() {}\n"
                "  async *stream() {}\n"
                "}\n"
                "export function plain() {}\n", encoding="utf-8")
            index = definitions(root)
        for name in ("Store", "request", "handle", "stream", "plain"):
            self.assertIn(name, index, name)
        for keyword in ("async", "private", "static", "override", "protected"):
            self.assertNotIn(keyword, index, keyword)


class CreditTests(unittest.TestCase):
    def test_sets_score_by_overlap_and_are_penalised_for_extras(self):
        gold = ["src/uu/fmt/src/linebreak.rs::break_lines", "src/uu/dd/src/diagnostics.rs::render"]
        self.assertEqual(credit(["linebreak::break_lines", "uu_dd::diagnostics::render"], gold, INDEX), 1.0)
        self.assertEqual(credit(["linebreak::break_lines"], gold, INDEX), 0.5)
        self.assertEqual(credit(["linebreak::break_lines", "uu_dd::diagnostics::render",
                                 "src/other.rs::extra"], gold, INDEX), 0.5)

    def test_null_gold_rewards_only_abstention(self):
        self.assertEqual(credit(None, None, INDEX), 1.0)
        self.assertEqual(credit("something", None, INDEX), 0.0)

    def test_answer_is_read_from_a_fenced_block_or_a_bare_object(self):
        self.assertEqual(answer_json('```json\n{"answer": 3}\n```'), 3)
        self.assertEqual(answer_json('{"answer": 3}'), 3)
        self.assertIsNone(answer_json("no answer here"))

    def test_decline_wording_is_detected(self):
        self.assertTrue(DECLINE.search("I cannot determine this without retrieval"))
        self.assertFalse(DECLINE.search("The answer is NumInfo::parse"))


if __name__ == "__main__":
    unittest.main()
