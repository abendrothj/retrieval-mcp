"""Fixture tests for the scale-response instrument. No model, no network, no server."""
import json
from pathlib import Path
import tempfile
import unittest

import scale_response


class RecordedCallsTests(unittest.TestCase):
    def archive(self, root, trial, task_id, system, status, items):
        directory = Path(root) / trial
        directory.mkdir(parents=True)
        (directory / "run.json").write_text(json.dumps(
            {"task_id": task_id, "system": system, "status": status}), encoding="utf-8")
        (directory / "codex-events.jsonl").write_text("\n".join(
            json.dumps({"type": "item.completed", "item": item}) for item in items),
            encoding="utf-8")

    def test_shell_and_tool_calls_are_collected_per_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.archive(tmp, "trial-0000", "q1", "native-control", "completed", [
                {"type": "command_execution", "command": "rg -n foo fs"},
                {"type": "agent_message", "text": "{}"}])
            self.archive(tmp, "trial-0001", "q1", "retrieval-mcp", "completed", [
                {"type": "mcp_tool_call", "tool": "search_exact", "arguments": {"pattern": "foo"}}])
            calls = scale_response.recorded_calls(tmp)
            self.assertEqual([entry["command"] for entry in calls["q1"]["shell"]], ["rg -n foo fs"])
            self.assertEqual(calls["q1"]["mcp"][0]["arguments"], {"pattern": "foo"})

    def test_a_failed_trial_contributes_no_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.archive(tmp, "trial-0000", "q1", "native-control", "failed", [
                {"type": "command_execution", "command": "rg -n foo fs"}])
            self.assertEqual(scale_response.recorded_calls(tmp), {})


class ReplayTests(unittest.TestCase):
    def test_a_replayed_command_runs_against_the_rung_and_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.c").write_text("void target(void) {}\n", encoding="utf-8")
            output, elapsed = scale_response.replay_shell("cat a.c", root, 30)
            self.assertIn("target", output)
            self.assertGreaterEqual(elapsed, 0)
            self.assertLessEqual(len(output), scale_response.SHELL_CAP)

    def test_the_same_command_sees_more_of_a_larger_rung(self):
        """The whole point of the ladder: behaviour fixed, corpus varying."""
        with tempfile.TemporaryDirectory() as tmp:
            small, large = Path(tmp) / "small", Path(tmp) / "large"
            for root in (small, large):
                root.mkdir()
                (root / "gold.c").write_text("void target(void) {}\n", encoding="utf-8")
            for index in range(20):
                (large / f"noise{index}.c").write_text("void target(void) {}\n", encoding="utf-8")
            small_out, _ = scale_response.replay_shell("grep -rn target .", small, 30)
            large_out, _ = scale_response.replay_shell("grep -rn target .", large, 30)
            self.assertLess(len(small_out), len(large_out))


class VisibilityTests(unittest.TestCase):
    def test_a_payload_proves_an_identity_only_when_both_halves_appear(self):
        pairs = [("fs/inode.c", "file_update_time")]
        self.assertEqual(scale_response.visible("fs/inode.c: file_update_time()", pairs), pairs)
        self.assertEqual(scale_response.visible("fs/inode.c: inode_update_time()", pairs), [])
        self.assertEqual(scale_response.visible("file_update_time in mm/oom.c", pairs), [])




class SealedReplayTests(unittest.TestCase):
    """The first ladder attempt stalled 29 minutes on ripgrep reading an inherited stdin."""

    def test_a_command_with_no_path_argument_does_not_block_on_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.c").write_text("void target(void) {}\n", encoding="utf-8")
            output, elapsed = scale_response.replay_shell(
                "cat", root, 10)  # reads stdin, and must see EOF at once
            self.assertEqual(output, "")
            self.assertLess(elapsed, 5000)

    def test_a_timeout_kills_the_pipeline_rather_than_orphaning_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, elapsed = scale_response.replay_shell("sleep 30 | cat", Path(tmp), 1)
            self.assertEqual(output, "")
            self.assertLess(elapsed, 10000)

    def test_a_login_shell_sees_a_scratch_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, home = Path(tmp) / "root", Path(tmp) / "home"
            root.mkdir()
            home.mkdir()
            output, _ = scale_response.replay_shell("/bin/zsh -lc 'echo $HOME'", root, 20, home)
            self.assertEqual(output.strip(), str(home))


if __name__ == "__main__":
    unittest.main()
