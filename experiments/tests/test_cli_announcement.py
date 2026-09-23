"""The CLI arm's prompt fragment: generated, matched, and free of MCP tool names."""
import subprocess
import unittest
from pathlib import Path

import cli_announcement

RELEASE = Path(__file__).resolve().parent.parent.parent / "target" / "release"
SERVER = RELEASE / "retrieval-mcp"
CLI = RELEASE / "retrieval"


class RenameTests(unittest.TestCase):
    def test_the_longer_tool_name_is_rewritten_before_its_shorter_namesake(self):
        """`search_exact` and `search_concept` share a prefix, and dict order is not length
        order. Rewriting shortest-first would leave `search_concept` mangled, and the arm would
        be announced with a subcommand that does not exist."""
        self.assertEqual(
            cli_announcement.rename("start with search_concept, not search_exact"),
            "start with retrieval concept, not retrieval search",
        )

    def test_the_four_default_tools_are_exactly_the_mapped_ones(self):
        self.assertEqual(
            set(cli_announcement.SUBCOMMANDS),
            {"search_exact", "read_source", "find_callers", "search_concept"},
        )

    def test_no_raw_tool_name_survives_a_rename(self):
        text = " ".join(cli_announcement.SUBCOMMANDS)
        renamed = cli_announcement.rename(text)
        for tool in cli_announcement.SUBCOMMANDS:
            self.assertNotIn(tool, renamed, f"{tool} was left un-rewritten")


class GeneratedAnnouncementTests(unittest.TestCase):
    """Needs both release binaries; skipped where they have not been built."""

    def setUp(self):
        if not (SERVER.is_file() and CLI.is_file()):
            self.skipTest("release binaries not built")

    def test_the_announcement_is_generated_matched_and_names_no_mcp_tool(self):
        text, mcp_side, help_text, tools, parts = cli_announcement.announcement(SERVER, CLI)

        # Generated: the synopsis and the routing text come from the binaries, not from prose
        # somebody wrote into the arm.
        self.assertIn(help_text.strip(), text)
        self.assertIn("Route repository retrieval by the question's intent", text)

        # Names no MCP tool: an arm told to "use search_exact" would be told to use something
        # that does not exist on its side.
        for tool in cli_announcement.SUBCOMMANDS:
            self.assertNotIn(tool, text, f"{tool} leaked into the CLI announcement")
        for subcommand in cli_announcement.SUBCOMMANDS.values():
            self.assertIn(subcommand, text)

        # Matched, and erring smaller: the CLI arm must not be handed more guidance than the
        # protocol hands the MCP arm, or a CLI win is a win for the bigger announcement.
        self.assertLessEqual(len(text), len(mcp_side))
        self.assertGreater(len(text), len(help_text) * 2,
                           "help alone is a ninth of MCP's announcement; that is not matched")
        self.assertEqual(sum(parts.values()), len(mcp_side))
        self.assertEqual(len(tools), 4)

    def test_it_makes_no_claim_that_the_command_is_better(self):
        """The leak this project audited was persuasive, not merely present: 'cheaper and more
        precise than reading files at random, and the first thing to reach for'."""
        text, *_ = cli_announcement.announcement(SERVER, CLI)
        lowered = text.lower()
        for claim in ("cheaper", "more precise", "faster", "better than", "instead of grep",
                      "first thing to reach for", "prefer this"):
            self.assertNotIn(claim, lowered, f"the announcement argues for itself: {claim!r}")


if __name__ == "__main__":
    unittest.main()
