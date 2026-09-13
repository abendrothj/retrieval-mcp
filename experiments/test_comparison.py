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
import quality_pass

HERE = Path(__file__).resolve()


def fake_server(name="native_search"):
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
                {"name": name, "description": "Return fixture evidence.",
                 "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}},
                {"name": f"{name}_admin", "description": "Must be filtered.",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}
        elif method == "tools/call":
            result = {"isError": False, "content": [{"type": "text", "text": f"fixture evidence from {name}"}],
                      "structuredContent": {"answer": "ok", "served_by": name}}
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
            names = [tool["name"] for tool in client.request("tools/list", {})["tools"]]
            if any(name.endswith("_admin") for name in names) or not names:
                raise ValueError("gate did not filter administrative tools")
            print(json.dumps({"type": "system", "subtype": "init",
                              "tools": [f"mcp__retrieval__{name}" for name in names],
                              "mcp_servers": [{"name": "retrieval", "status": "connected"}]}), flush=True)
            # Call every exposed tool so a bundled arm exercises both upstreams on one budget.
            for index, name in enumerate(names, 1):
                print(json.dumps({"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": str(index), "name": f"mcp__retrieval__{name}",
                    "input": {"query": "fixture"}}]}}), flush=True)
                result = client.request("tools/call", {"name": name, "arguments": {"query": "fixture"}})
                print(json.dumps({"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": str(index), "content": result}]}}), flush=True)
            print(json.dumps({"type": "result", "is_error": False, "result": answer,
                              "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
        finally:
            client.close()


def broke_agent():
    """An agent whose provider refuses to serve: the shape opencode_wrapper emits for a 402."""
    print(json.dumps({"type": "result", "is_error": True, "subtype": "provider_error:402",
                      "provider_detail": "Insufficient Balance", "result": None,
                      "usage": {}}), flush=True)


def upstream(identifier, tool):
    return {
        "id": identifier,
        "command": [sys.executable, str(HERE), "--fake-server", tool, "{listen}"],
        "environment": {},
        "visible_tools": [tool],
        "expected_upstream_tools": [tool, f"{tool}_admin"],
    }


def systems(path):
    single = {
        "mcp_enabled": True, "upstreams": [upstream("one", "native_search")],
        "environment": {}, "prompt_policy": "",
        "prepare_commands": [], "check_commands": [], "version_command": [sys.executable, "--version"],
    }
    bundle = {
        "id": "bundle", "mcp_enabled": True,
        "upstreams": [upstream("one", "native_search"), upstream("two", "graph_search")],
        "environment": {}, "prompt_policy": "Route lexical questions to native_search.",
        "prepare_commands": [], "check_commands": [], "version_command": None,
    }
    control = {
        "id": "native-control", "mcp_enabled": False, "upstreams": [], "environment": {},
        "prompt_policy": "", "prepare_commands": [],
        "check_commands": [], "version_command": [sys.executable, "--version"],
    }
    path.write_text(json.dumps({"version": "comparison-systems-v2", "systems": [
        control, {"id": "alpha", **single}, {"id": "beta", **single}, bundle,
    ]}))


def versioned_systems(path, pinned_binary):
    """Two MCP arms whose only difference is the server binary each one pins."""
    def arm(identifier, server=None):
        entry = {
            "id": identifier, "mcp_enabled": True,
            "upstreams": [{
                "id": "one",
                "command": ["{server}", "--fake-server", "native_search", "{listen}"],
                "environment": {},
                "visible_tools": ["native_search"],
                "expected_upstream_tools": ["native_search", "native_search_admin"],
            }],
            "environment": {}, "prompt_policy": "Shared policy.",
            "prepare_commands": [[sys.executable, "-c", "import sys; print(sys.argv[1])", "{server}"]],
            "check_commands": [], "version_command": None,
        }
        if server is not None:
            entry["server"] = str(server)
        return entry
    path.write_text(json.dumps({"version": "comparison-systems-v2", "systems": [
        arm("default-server"), arm("pinned-server", pinned_binary),
    ]}))


class ComparisonTests(unittest.TestCase):
    def test_balanced_plan_uses_question_hash_and_policy_in_the_prompt(self):
        tasks = [{"id": "q", "question": "Question?", "expected_json": {"answer": "ok"}}]
        configured = [{"id": "alpha", "prompt_policy": ""},
                      {"id": "beta", "prompt_policy": "Route callers to graph_search."}]
        roots = {"alpha": "/tmp/alpha", "beta": "/tmp/alpha"}
        first = comparison_runner.make_plan(tasks, configured, roots, 2, 42)
        second = comparison_runner.make_plan(tasks, configured, roots, 2, 42)
        self.assertEqual(first, second)
        self.assertEqual(first["planned_trials"], 4)
        self.assertEqual(len({trial["question_sha256"] for trial in first["trials"]}), 1)
        # Same root, so only the routing policy can separate the two prompts.
        self.assertEqual(len({trial["prompt_sha256"] for trial in first["trials"]}), 2)
        routed = next(t for t in first["trials"] if t["system"] == "beta")
        self.assertIn("Route callers to graph_search.", routed["prompt"])
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
            self.assertEqual(len(prepared["systems"]), 4)
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
            self.assertEqual(result["completed"], 4)
            listen_addresses = []
            for run_path in output.glob("trial-*/run.json"):
                run = json.loads(run_path.read_text())
                self.assertTrue(run["payload_matches"])
                self.assertTrue(run["resolved_correct"])
                tools_path = run_path.parent / "tools.json"
                if run["system"] == "native-control":
                    self.assertEqual(run["tool_sequence"], [])
                    self.assertFalse(tools_path.exists())
                    continue
                gate = json.loads((run_path.parent / "gate.json").read_text())
                listen_addresses += [upstream["command"][-1] for upstream in gate["upstreams"]]
                names = [tool["name"] for tool in json.loads(tools_path.read_text())["tools"]]
                if run["system"] == "bundle":
                    # Both servers are exposed, both are called, and both share one budget.
                    self.assertEqual(names, ["native_search", "graph_search"])
                    self.assertEqual(run["tool_sequence"], ["native_search", "graph_search"])
                    self.assertEqual(run["upstream_calls"], {"one": 1, "two": 1})
                else:
                    self.assertEqual(names, ["native_search"])
                    self.assertEqual(run["tool_sequence"], ["native_search"])
                    self.assertEqual(run["upstream_calls"], {"one": 1})
            # Every upstream process, including both halves of the bundle, gets its own port.
            self.assertEqual(len(set(listen_addresses)), 4)
            for address in listen_addresses:
                host, port = address.rsplit(":", 1)
                self.assertEqual(host, "127.0.0.1")
                self.assertGreater(int(port), 0)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["model"], "fixture-model")
            self.assertEqual(manifest["variant"], "high")
            report = analyze_comparison.analyze(output)
            self.assertEqual([system["payload_correct"] for system in report["systems"]], [1, 1, 1, 1])
            self.assertEqual([system["resolved_correct"] for system in report["systems"]], [1, 1, 1, 1])
            self.assertEqual(len(report["pairs"]), 6)
            self.assertTrue(all(pair["eligible"] for pair in report["pairs"]))
            costed = next(s for s in report["systems"] if s["system"] == "bundle")
            # Tokens and cost are paired factors, not just wall-clock and bytes.
            self.assertEqual(costed["metrics"]["output_tokens"]["mean"], 1)
            self.assertIsNotNone(costed["metrics"]["input_tokens"]["mean"])
            self.assertEqual(costed["cost_usd_total"], 0)
            self.assertEqual(costed["cost_usd_per_resolved"], 0)

    def test_model_use_requires_both_explicit_gates(self):
        with self.assertRaisesRegex(ValueError, "allow-model-usage"):
            comparison_runner.run(SimpleNamespace(client="claude", allow_model_usage=False, model="x"))

    def test_a_provider_outage_aborts_the_whole_run_and_marks_it_incomplete(self):
        """Billing and quota failures are not results about retrieval; they must stop everything."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"; source.mkdir()
            (source / "evidence.txt").write_text("fixture evidence\n")
            systems_path = base / "systems.json"; systems(systems_path)
            questions = base / "questions.json"
            questions.write_text(json.dumps([{"id": "q", "question": "Return the fixture answer.",
                                              "expected_json": {"answer": "ok"}}]))
            workspace = base / "workspace"
            comparison_runner.prepare(SimpleNamespace(
                source_root=source, workspace=workspace, systems=systems_path,
                server=Path(sys.executable), semantic_command=["fixture"], prepare_timeout=30))
            output = base / "results"
            with self.assertRaisesRegex(RuntimeError, "provider failure"):
                comparison_runner.run(SimpleNamespace(
                    client="command", allow_model_usage=True, model="fixture-model", variant=None,
                    agent_command=[sys.executable, str(HERE), "--broke-agent"],
                    max_budget_usd=1, timeout=30, tool_timeout=20, max_calls=3, max_bytes=10000,
                    workspace=workspace, output=output, systems=systems_path,
                    questions=questions, server=Path(sys.executable), semantic_command=["fixture"],
                    repetitions=1, seed=42))
            status = json.loads((output / "status.json").read_text())
            self.assertFalse(status["complete"])
            self.assertEqual(status["completed"], 0)
            self.assertEqual(status["aborted"]["reason"], "provider_error:402")
            # Exactly one trial ran: the outage stopped the plan instead of burning three per arm.
            states = [json.loads(path.read_text()) for path in output.glob("trial-*/run.json")]
            self.assertEqual([state["status"] for state in states], ["provider_error"])


    def test_quality_axis_scores_payload_independently_of_the_envelope(self):
        task = {"id": "q", "question": "?", "expected_json": {"answer": "src/a.rs::run"}}
        prose = 'The helper is `run`.\n\n{"answer": "src/a.rs::run"}'
        # The frozen grader still fails the envelope, but the quality axis must see the payload.
        graded = benchmark.grade_answer(task, prose, "json-answer-v3")
        self.assertFalse(graded["format_correct"])
        self.assertFalse(graded["correct"])
        self.assertEqual(quality_pass.answer_json(prose), "src/a.rs::run")
        self.assertEqual(quality_pass.credit(quality_pass.answer_json(prose),
                                             task["expected_json"]["answer"], {}), 1.0)
        quoted = 'Source:\n```rust\nfn run() {}\n```\n```json\n{"answer": "src/a.rs::run"}\n```'
        self.assertEqual(quality_pass.answer_json(quoted), "src/a.rs::run")
        self.assertIsNone(quality_pass.answer_json('{"result": "src/a.rs::run"}'))

    def test_each_arm_prepares_against_the_server_binary_it_pins(self):
        """Two versions of one server are only comparable if each arm gets its own binary."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"; source.mkdir()
            (source / "evidence.txt").write_text("fixture evidence\n")
            pinned = base / "retrieval-mcp-v0.1.1"
            pinned.write_bytes(b"#!/bin/sh\nexit 0\n")
            resolved = pinned.resolve()
            systems_path = base / "systems.json"; versioned_systems(systems_path, pinned)
            workspace = base / "workspace"
            default = Path(sys.executable).resolve()
            prepared = comparison_runner.prepare(SimpleNamespace(
                source_root=source, workspace=workspace, systems=systems_path,
                server=Path(sys.executable), semantic_command=["fixture"], prepare_timeout=30))
            records = {record["id"]: record for record in prepared["systems"]}
            # The pinned arm overrides {server} everywhere: upstream, prepare command, and manifest.
            override = records["pinned-server"]
            self.assertEqual(override["server"], str(resolved))
            self.assertEqual(override["server_sha256"], comparison_runner.digest(pinned))
            self.assertEqual(override["upstreams"][0]["command_executable"], str(resolved))
            self.assertEqual(override["commands"][0]["command"][-1], str(resolved))
            # Without the field the run-wide --server still decides, exactly as before.
            unpinned = records["default-server"]
            self.assertEqual(unpinned["server"], str(default))
            self.assertEqual(unpinned["server_sha256"], comparison_runner.digest(default))
            self.assertEqual(unpinned["upstreams"][0]["command_executable"], str(default))
            self.assertEqual(unpinned["commands"][0]["command"][-1], str(default))
            self.assertEqual(comparison_runner.validate_prepared(
                workspace, systems_path, Path(sys.executable), ["fixture"])["version"],
                "comparison-prepared-v2")
            pinned.write_bytes(b"#!/bin/sh\nexit 1\n")
            with self.assertRaisesRegex(ValueError, "server binary changed after preparation"):
                comparison_runner.validate_prepared(
                    workspace, systems_path, Path(sys.executable), ["fixture"])

    def test_a_pinned_server_path_may_be_relative_to_the_repository(self):
        system = {"id": "pinned", "server": "experiments/comparison_runner.py"}
        self.assertEqual(comparison_runner.system_server(system, Path(sys.executable)), HERE.parent / "comparison_runner.py")
        self.assertEqual(comparison_runner.system_server({"id": "plain"}, Path(sys.executable)),
                         Path(sys.executable))
        with self.assertRaises(FileNotFoundError):
            comparison_runner.system_server({"id": "gone", "server": "runs/no-such-binary"}, Path(sys.executable))

    def test_the_perf_v020_systems_file_differs_only_by_binary_between_retrieval_arms(self):
        document = comparison_runner.load_systems(HERE.parent / "comparison_systems_perf_v020.json")
        arms = {system["id"]: system for system in document["systems"]}
        self.assertEqual(list(arms), ["native-control", "zvec-grep", "retrieval-v011", "retrieval-v020"])
        first, second = arms["retrieval-v011"], arms["retrieval-v020"]
        self.assertEqual(comparison_runner.visible_tools(first), comparison_runner.visible_tools(second))
        self.assertEqual(first["prompt_policy"], second["prompt_policy"])
        self.assertEqual({key: value for key, value in first.items() if key not in ("id", "server")},
                         {key: value for key, value in second.items() if key not in ("id", "server")})
        self.assertNotEqual(first["server"], second["server"])
        held_out = comparison_runner.load_systems(HERE.parent / "comparison_systems_heldout.json")
        retrieval = next(s for s in held_out["systems"] if s["id"] == "retrieval-mcp")
        self.assertEqual(first["prompt_policy"], retrieval["prompt_policy"])
        # No registry access during preparation: the zvec arm must copy a cached install, never install one.
        commands = arms["zvec-grep"]["prepare_commands"]
        self.assertFalse(any(part == "npm" for command in commands for part in command))
        self.assertIn("shutil.copytree", commands[0][2])

    def test_a_native_control_may_not_pin_a_server_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "systems.json"
            systems(path)
            document = json.loads(path.read_text())
            document["systems"][0]["server"] = "target/release/retrieval-mcp"
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "must not pin a server binary"):
                comparison_runner.load_systems(path)
            document["systems"][0].pop("server")
            document["systems"][1]["binary"] = "target/release/retrieval-mcp"
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "unexpected fields"):
                comparison_runner.load_systems(path)

if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-server"]:
        fake_server(sys.argv[2])
    elif sys.argv[1:2] == ["--fake-agent"]:
        fake_agent(sys.argv[2], sys.argv[3])
    elif sys.argv[1:2] == ["--broke-agent"]:
        broke_agent()
    else:
        unittest.main()
