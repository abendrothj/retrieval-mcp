import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

import analyze_comparison
import benchmark
import comparison_runner

HERE = Path(__file__).resolve()


def fake_server():
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "comparison-fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": "native_search", "description": "Return fixture evidence.",
                 "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}},
                {"name": "admin_tool", "description": "Must be filtered.",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}
        elif method == "tools/call":
            result = {"isError": False, "content": [{"type": "text", "text": "fixture evidence"}],
                      "structuredContent": {"answer": "ok"}}
        elif method == "ping":
            result = {}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "unsupported"}]}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def fake_agent(config_path, answer):
    servers = json.loads(Path(config_path).read_text())["mcpServers"]
    if "retrieval" not in servers:
        print(json.dumps({"type": "result", "is_error": False, "result": answer,
                          "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
        return
    server = servers["retrieval"]
    with open(os.devnull, "w") as stderr:
        client = benchmark.MCP([server["command"], *server["args"]], os.environ.copy(), Path.cwd(), stderr, 20)
        try:
            client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "comparison-agent-fixture", "version": "1"}})
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = client.request("tools/list", {})["tools"]
            if [tool["name"] for tool in tools] != ["native_search"]:
                raise ValueError("gate did not filter administrative tools")
            print(json.dumps({"type": "system", "subtype": "init",
                              "tools": ["mcp__retrieval__native_search"],
                              "mcp_servers": [{"name": "retrieval", "status": "connected"}]}), flush=True)
            print(json.dumps({"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "1", "name": "mcp__retrieval__native_search",
                "input": {"query": "fixture"}}]}}), flush=True)
            result = client.request("tools/call", {"name": "native_search", "arguments": {"query": "fixture"}})
            print(json.dumps({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "1", "content": result}]}}), flush=True)
            print(json.dumps({"type": "result", "is_error": False, "result": answer,
                              "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
        finally:
            client.close()


def systems(path):
    system = {
        "mcp_enabled": True,
        "command": [sys.executable, str(HERE), "--fake-server", "{listen}"],
        "environment": {}, "visible_tools": ["native_search"],
        "expected_upstream_tools": ["native_search", "admin_tool"],
        "prepare_commands": [], "check_commands": [], "version_command": [sys.executable, "--version"],
    }
    control = {
        "id": "native-control", "mcp_enabled": False, "command": [], "environment": {},
        "visible_tools": [], "expected_upstream_tools": [], "prepare_commands": [],
        "check_commands": [], "version_command": [sys.executable, "--version"],
    }
    path.write_text(json.dumps({"version": "comparison-systems-v1", "systems": [
        control, {"id": "alpha", **system}, {"id": "beta", **system},
    ]}))


class ComparisonTests(unittest.TestCase):
    def test_balanced_plan_uses_question_hash_for_cross_root_pairing(self):
        tasks = [{"id": "q", "question": "Question?", "expected_json": {"answer": "ok"}}]
        configured = [{"id": "alpha"}, {"id": "beta"}]
        roots = {"alpha": "/tmp/alpha", "beta": "/tmp/beta"}
        first = comparison_runner.make_plan(tasks, configured, roots, 2, 42)
        second = comparison_runner.make_plan(tasks, configured, roots, 2, 42)
        self.assertEqual(first, second)
        self.assertEqual(first["planned_trials"], 4)
        self.assertEqual(len({trial["question_sha256"] for trial in first["trials"]}), 1)
        self.assertEqual(len({trial["prompt_sha256"] for trial in first["trials"]}), 2)
    def test_failed_preparation_preserves_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"; source.mkdir()
            (source / "evidence.txt").write_text("fixture evidence\n")
            systems_path = base / "systems.json"; systems(systems_path)
            document = json.loads(systems_path.read_text())
            document["systems"][1]["prepare_commands"] = [
                [sys.executable, "-c", "import sys; print('failed', file=sys.stderr); sys.exit(2)"]]
            systems_path.write_text(json.dumps(document))
            workspace = base / "workspace"
            with self.assertRaisesRegex(RuntimeError, "preparation command failed"):
                comparison_runner.prepare(SimpleNamespace(
                    source_root=source, workspace=workspace, systems=systems_path,
                    server=Path(sys.executable), semantic_command=["fixture"], prepare_timeout=30))
            self.assertTrue((workspace / "failed.json").is_file())
            stderr = workspace / "systems" / "alpha" / "state" / "command-01.stderr.log"
            self.assertIn("failed", stderr.read_text())


    def test_prepare_run_gate_and_analysis_without_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"; source.mkdir()
            (source / "evidence.txt").write_text("fixture evidence\n")
            systems_path = base / "systems.json"; systems(systems_path)
            questions = base / "questions.json"
            questions.write_text(json.dumps([{"id": "q", "question": "Return the fixture answer.",
                                               "expected_json": {"answer": "ok"}}]))
            workspace = base / "workspace"
            prepared = comparison_runner.prepare(SimpleNamespace(
                source_root=source, workspace=workspace, systems=systems_path,
                server=Path(sys.executable), semantic_command=["fixture"], prepare_timeout=30))
            self.assertEqual(len(prepared["systems"]), 3)
            dry_output = base / "dry-run"
            dry = comparison_runner.run(SimpleNamespace(
                client="claude", allow_model_usage=False, model=None, agent_command=None,
                max_budget_usd=1, timeout=30, tool_timeout=20, max_calls=3, max_bytes=10000,
                workspace=workspace, output=dry_output, systems=systems_path,
                questions=questions, server=Path(sys.executable), semantic_command=["fixture"],
                repetitions=1, seed=42, dry_run=True))
            self.assertTrue(dry["dry_run"])
            self.assertFalse(list(dry_output.glob("trial-*")))
            output = base / "results"
            answer = json.dumps({"answer": "ok"})
            result = comparison_runner.run(SimpleNamespace(
                client="command", allow_model_usage=True, model="fixture-model", variant="high",
                agent_command=[sys.executable, str(HERE), "--fake-agent", "{mcp_config}", answer],
                max_budget_usd=1, timeout=30, tool_timeout=20, max_calls=3, max_bytes=10000,
                workspace=workspace, output=output, systems=systems_path,
                questions=questions, server=Path(sys.executable), semantic_command=["fixture"],
                repetitions=1, seed=42))
            self.assertEqual(result["completed"], 3)
            listen_addresses = []
            for run_path in output.glob("trial-*/run.json"):
                run = json.loads(run_path.read_text())
                self.assertTrue(run["payload_matches"])
                self.assertTrue(run["resolved_correct"])
                if run["system"] == "native-control":
                    self.assertEqual(run["tool_sequence"], [])
                    self.assertFalse((run_path.parent / "tools.json").exists())
                else:
                    self.assertEqual(run["tool_sequence"], ["native_search"])
                    tools = json.loads((run_path.parent / "tools.json").read_text())
                    self.assertEqual([tool["name"] for tool in tools["tools"]], ["native_search"])
                    gate = json.loads((run_path.parent / "gate.json").read_text())
                    listen_addresses.append(gate["command"][-1])
            self.assertEqual(len(set(listen_addresses)), 2)
            for address in listen_addresses:
                host, port = address.rsplit(":", 1)
                self.assertEqual(host, "127.0.0.1")
                self.assertGreater(int(port), 0)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["model"], "fixture-model")
            self.assertEqual(manifest["variant"], "high")
            report = analyze_comparison.analyze(output)
            self.assertEqual([system["payload_correct"] for system in report["systems"]], [1, 1, 1])
            self.assertEqual([system["resolved_correct"] for system in report["systems"]], [1, 1, 1])
            self.assertEqual(len(report["pairs"]), 3)
            self.assertTrue(all(pair["eligible"] for pair in report["pairs"]))

    def test_model_use_requires_both_explicit_gates(self):
        with self.assertRaisesRegex(ValueError, "allow-model-usage"):
            comparison_runner.run(SimpleNamespace(client="claude", allow_model_usage=False, model="x"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-server"]:
        fake_server()
    elif sys.argv[1:2] == ["--fake-agent"]:
        fake_agent(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
