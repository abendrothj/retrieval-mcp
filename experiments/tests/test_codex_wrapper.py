"""Fixture tests for the Codex wrapper: event parsing and session isolation. No model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from codex_wrapper import parse_events


def command_event(command):
    return {"type": "item.completed",
            "item": {"type": "command_execution", "command": command, "aggregated_output": ""}}


class CodexParserTests(unittest.TestCase):
    def test_a_shell_read_outside_the_corpus_is_contamination(self):
        """The defect that stopped runs/linux-agent-20260919 on its seventh trial.

        Both arms opened a skill document describing the tools under test, from the operator's
        home directory, as their first command. It is evidence no corpus contains.
        """
        outcome = parse_events(
            [command_event('/bin/zsh -lc "sed -n 1,240p /Users/ja/.agents/skills/retrieval-mcp/SKILL.md"')],
            "/tmp/corpus")
        self.assertEqual(outcome["unexpected_tools"], ["read_outside_corpus"])

    def test_work_inside_the_corpus_and_the_usual_binaries_stay_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            (corpus / "fs").mkdir(parents=True)
            outcome = parse_events([
                command_event('/bin/zsh -lc "rg -n nsm_monitor fs/lockd"'),
                command_event(f'/bin/zsh -lc "sed -n 1,40p {corpus}/fs/lockd/mon.c"'),
                command_event('/bin/zsh -lc "grep -rn foo . 2>/dev/null"'),
            ], str(corpus))
            self.assertEqual(outcome["unexpected_tools"], [])

    def test_without_a_corpus_root_no_path_is_judged(self):
        outcome = parse_events([command_event("sed -n 1,10p /etc/passwd")], None)
        self.assertEqual(outcome["unexpected_tools"], [])
        self.assertEqual(len(outcome["tool_calls"]), 1)

    def test_mcp_calls_and_usage_survive_the_translation(self):
        events = [
            {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "retrieval",
                                                "tool": "search_exact", "result": "hit"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer": "ok"}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3,
                                                 "cached_input_tokens": 7}},
        ]
        outcome = parse_events(events, "/tmp/corpus")
        self.assertEqual(outcome["final"], '{"answer": "ok"}')
        self.assertEqual([call["name"] for call in outcome["tool_calls"]], ["search_exact"])
        self.assertEqual(outcome["usage"]["input_tokens"], 11)
        self.assertEqual(outcome["usage"]["cache_read_input_tokens"], 7)
        self.assertEqual(outcome["unexpected_tools"], [])

    def test_a_second_mcp_server_is_contamination(self):
        outcome = parse_events([{"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "something-else", "tool": "search"}}], "/tmp/corpus")
        self.assertEqual(outcome["unexpected_tools"], ["other_mcp_server"])


class CodexIsolationTests(unittest.TestCase):
    def test_the_trial_runs_with_its_own_home_so_machine_skills_are_invisible(self):
        """HOME, not just CODEX_HOME: `--ignore-user-config` never covered ~/.agents/skills."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = root / "corpus"
            corpus.mkdir()
            run_dir = root / "run"
            run_dir.mkdir()
            prompt = root / "prompt.txt"
            prompt.write_text("Return JSON.", encoding="utf-8")
            mcp = root / "mcp.json"
            mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            binary = root / "bin"
            binary.mkdir()
            fake = binary / "codex"
            fake.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json, os, sys
                home = os.environ["HOME"]
                assert home == os.environ["CODEX_HOME"], (home, os.environ["CODEX_HOME"])
                assert home.endswith("codex-home"), home
                assert os.environ["XDG_CONFIG_HOME"].startswith(home)
                assert "--ignore-user-config" in sys.argv
                print(json.dumps({"type": "item.completed",
                                  "item": {"type": "agent_message", "text": '{"answer": "ok"}'}}))
                print(json.dumps({"type": "turn.completed",
                                  "usage": {"input_tokens": 5, "output_tokens": 1}}))
                """))
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{binary}{os.pathsep}{os.environ['PATH']}")
            wrapper = Path(__file__).resolve().parents[1] / "codex_wrapper.py"
            process = subprocess.run(
                [sys.executable, str(wrapper), "gpt-5.6-luna", str(mcp), str(prompt), str(run_dir)],
                cwd=corpus, env=env, capture_output=True, text=True, check=True)
            result = json.loads(process.stdout.splitlines()[-1])
            self.assertFalse(result["is_error"])
            self.assertEqual(result["result"], '{"answer": "ok"}')
            self.assertTrue((run_dir / "codex-home" / "auth.json").is_file()
                            or not (Path.home() / ".codex" / "auth.json").is_file())


if __name__ == "__main__":
    unittest.main()
