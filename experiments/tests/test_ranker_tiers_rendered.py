"""The rendered-table input format, which an archived run's payload uses instead of JSON.

Applied to 150 trials of a real run, the structured-only version of this instrument returned
"neither" 150 times beside 56 of 75 resolving - impossible, and the reason is that the server
stopped emitting `structuredContent` and the payload carries a tab-separated table instead.
"""
import unittest

import ranker_tiers

GOLD = "django/core/management/commands/runserver.py::Command::on_bind"
PAGE = (
    "backend: bm25/symbol-chunks\n"
    "indexed_files: 400\n"
    "results[2] end_line\tpath\tscore\tstart_line\tsymbol\n"
    "330\tdjango/core/management/commands/runserver.py\t16.8\t300\t"
    '{"kind":"function_definition","symbol":'
    '"django/core/management/commands/runserver.py::inner_run"}\n'
    "120\tother/thing.py\t9.1\t100\t"
    '{"kind":"function_definition","symbol":"other/thing.py::f"}\n'
)


class RenderedRows(unittest.TestCase):
    def test_rows_come_out_of_the_tab_separated_table(self):
        rows = ranker_tiers.rendered_rows(PAGE)
        self.assertEqual([r["path"] for r in rows],
                         ["django/core/management/commands/runserver.py", "other/thing.py"])

    def test_the_identity_is_read_from_the_servers_own_json_cell(self):
        rows = ranker_tiers.rendered_rows(PAGE)
        self.assertEqual(rows[0]["identity"],
                         "django/core/management/commands/runserver.py::inner_run")

    def test_a_sibling_definition_still_scores_file_only(self):
        rows = ranker_tiers.rendered_rows(PAGE)
        identity, file_rank = ranker_tiers.positions(rows, GOLD)
        self.assertIsNone(identity)
        self.assertEqual(file_rank, 1)
        self.assertEqual(ranker_tiers.tier(identity, file_rank), "file_only")

    def test_a_page_with_no_results_block_yields_nothing(self):
        self.assertEqual(ranker_tiers.rendered_rows("backend: bm25\nhas_more: false\n"), [])

    def test_a_caller_row_uses_its_caller_column(self):
        page = ("results[1] caller\tcolumn\tline\tname\tpath\n"
                "TestLessor\t4\t275\tCheckpoint\tserver/lease/lessor_test.go\n")
        rows = ranker_tiers.rendered_rows(page)
        self.assertEqual(rows[0]["identity"], "TestLessor")
        self.assertEqual(rows[0]["path"], "server/lease/lessor_test.go")
