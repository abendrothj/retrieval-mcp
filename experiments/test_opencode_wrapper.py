"""Fixture tests for the OpenCode event-stream parser; no model or MCP calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from opencode_wrapper import classify_tool, parse_events


def event(kind, **properties):
    return {"type": kind, **properties}


class OpenCodeParserTests(unittest.TestCase):
    def test_final_answer_comes_from_the_last_text_producing_message(self):
        events = [
            event("step_start", part={"type": "step-start", "messageID": "msg1"}),
            event("text", part={"type": "text", "messageID": "msg1", "id": "t1", "text": "Let me look.\n"}),
            event("tool_use", part={
                "type": "tool", "messageID": "msg1", "callID": "c1",
                "tool": "retrieval_search_exact",
                "state": {"status": "completed", "input": {"query": "fixture"}, "output": "evidence"},
            }),
            event("step_finish", part={
                "type": "step-finish", "messageID": "msg1", "reason": "tool-calls", "cost": 0.01,
                "tokens": {"input": 100, "output": 20, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            }),
            event("step_start", part={"type": "step-start", "messageID": "msg2"}),
            event("text", part={"type": "text", "messageID": "msg2", "id": "t2",
                                "text": '{"answer": "ok"}\n'}),
            event("step_finish", part={
                "type": "step-finish", "messageID": "msg2", "reason": "stop", "cost": 0.02,
                "tokens": {"input": 150, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            }),
        ]
        outcome = parse_events(events, {"search_exact"})
        self.assertEqual(outcome["final"], '{"answer": "ok"}\n')
        self.assertEqual(outcome["usage"]["input_tokens"], 250)
        self.assertEqual(outcome["usage"]["output_tokens"], 30)
        self.assertAlmostEqual(outcome["usage"]["cost_usd"], 0.03)

    def test_multi_part_answer_is_joined(self):
        events = [
            event("step_start", part={"type": "step-start", "messageID": "msg1"}),
            event("text", part={"type": "text", "messageID": "msg1", "id": "t1", "text": "a"}),
            event("text", part={"type": "text", "messageID": "msg1", "id": "t2", "text": "b"}),
            event("step_finish", part={
                "type": "step-finish", "messageID": "msg1", "reason": "stop", "cost": 0.0,
                "tokens": {"input": 1, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            }),
        ]
        self.assertEqual(parse_events(events, set())["final"], "ab")

    def test_tool_classification(self):
        retrieval = {"search_exact", "search_graph"}
        self.assertEqual(classify_tool("retrieval_search_exact", retrieval), "retrieval")
        self.assertEqual(classify_tool("search_graph", retrieval), "retrieval")
        self.assertEqual(classify_tool("read", retrieval), "native")
        self.assertEqual(classify_tool("bash", retrieval), "native")
        self.assertEqual(classify_tool("web_search", retrieval), "contamination")
        self.assertEqual(classify_tool("edit", retrieval), "contamination")

    def test_wrapper_isolates_config_and_translates_cli_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"; corpus.mkdir()
            run_dir = root / "run"; run_dir.mkdir()
            prompt = root / "prompt.txt"; prompt.write_text("Return JSON.")
            gate = root / "gate.json"
            gate.write_text(json.dumps({"upstreams": [
                {"id": "one", "visible_tools": ["search_exact"]},
                {"id": "two", "visible_tools": ["search_graph"]},
            ]}))
            mcp = root / "mcp.json"
            mcp.write_text(json.dumps({"mcpServers": {"retrieval": {
                "command": sys.executable, "args": ["comparison_gate.py", "--config", str(gate)],
            }}}))
            binary = root / "bin"; binary.mkdir()
            fake = binary / "opencode"
            fake.write_text(textwrap.dedent("""\
                #!/usr/bin/env python3
                import json, os, sys
                from pathlib import Path
                config = json.loads((Path(os.environ["OPENCODE_CONFIG_DIR"]) / "opencode.json").read_text())
                assert set(config["mcp"]) == {"retrieval"}
                assert config["permission"]["*"] == "deny"
                assert config["permission"]["read"] == "allow"
                assert config["permission"]["retrieval_search_exact"] == "allow"
                assert config["permission"]["retrieval_search_graph"] == "allow"
                assert "codebase-memory-mcp" not in config["mcp"]
                assert Path(os.environ["XDG_CONFIG_HOME"]).parent == Path(os.environ["OPENCODE_CONFIG_DIR"]).parent
                assert sys.argv[sys.argv.index("--model") + 1] == "deepseek/deepseek-v4-flash"
                assert sys.argv[sys.argv.index("--variant") + 1] == "high"
                assert sys.argv[-1] == "Return JSON."
                print(json.dumps({"type": "step_start", "part": {"messageID": "msg1"}}))
                print(json.dumps({"type": "text", "part": {"messageID": "msg1", "text": json.dumps({"answer": "ok"})}}))
                print(json.dumps({"type": "step_finish", "part": {"messageID": "msg1", "cost": 0.01,
                    "tokens": {"input": 2, "output": 3, "reasoning": 1, "cache": {"read": 0, "write": 0}}}}))
                """))
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
                       COMPARISON_OPENCODE_VARIANT="high")
            wrapper = Path(__file__).with_name("opencode_wrapper.py")
            process = subprocess.run(
                [sys.executable, str(wrapper), "deepseek/deepseek-v4-flash",
                 str(mcp), str(prompt), str(run_dir)],
                cwd=corpus, env=env, capture_output=True, text=True, check=True)
            result = json.loads(process.stdout.splitlines()[-1])
            self.assertFalse(result["is_error"])
            self.assertEqual(result["result"], '{"answer": "ok"}')
            self.assertEqual(result["usage"]["input_tokens"], 2)
            self.assertTrue((run_dir / "opencode-events.jsonl").is_file())

    def test_contamination_and_error_are_flagged(self):
        events = [
            event("step_start", part={"type": "step-start", "messageID": "msg1"}),
            event("tool_use", part={
                "type": "tool", "messageID": "msg1", "callID": "c1", "tool": "web_search",
                "state": {"status": "completed", "input": {}, "output": "external"},
            }),
            event("tool_use", part={
                "type": "tool", "messageID": "msg1", "callID": "c2", "tool": "retrieval_search_exact",
                "state": {"status": "completed", "input": {}, "output": "evidence"},
            }),
            event("error", error={"name": "APIError", "data": {"message": "failed"}}),
        ]
        outcome = parse_events(events, {"search_exact"})
        self.assertEqual(outcome["unexpected_tools"], ["web_search"])
        self.assertEqual(outcome["client_error"], "APIError")
        self.assertIsNone(outcome["final"])

    def test_a_provider_outage_is_distinguished_from_a_model_error(self):
        """A 402 or a quota message must be reported as the provider's failure, not the arm's."""
        for error, expected in (
            ({"name": "APIError", "data": {"message": "Insufficient Balance", "statusCode": 402}},
             "provider_error:402"),
            ({"name": "APIError", "data": {"message": "rate limit exceeded", "statusCode": 429}},
             "provider_error:429"),
            ({"name": "AuthError", "data": {"message": "invalid api key"}},
             "provider_error:AuthError"),
            ({"name": "APIError", "data": {"message": "model produced invalid tool call"}},
             "APIError"),
        ):
            outcome = parse_events([event("error", error=error)], set())
            self.assertEqual(outcome["client_error"], expected, error)
        outcome = parse_events([event("error", error={"name": "APIError", "data": {
            "message": "Insufficient Balance", "statusCode": 402}})], set())
        self.assertEqual(outcome["provider_detail"], "Insufficient Balance")


if __name__ == "__main__":
    unittest.main()
