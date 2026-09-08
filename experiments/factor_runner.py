#!/usr/bin/env python3
"""Factor runner with explicit model-use gate; scripted mode uses real MCP, no inference."""
import argparse
import ast
import fcntl
import hashlib
import json
import os
import math
from pathlib import Path
import subprocess
import shutil
import sys
import time
from types import SimpleNamespace

import benchmark
from plan_factors import plan

GRADING = "json-answer-v3"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scripted(config_path, policy):
    if config_path == "none":
        # Tool-free control: the fixture has no priors, so it abstains rather than inventing one.
        print(json.dumps({"type":"result", "is_error":False,
              "result":json.dumps({"answer":None}), "test_only":True}), flush=True)
        return
    config = json.loads(Path(config_path).read_text())["mcpServers"]["retrieval"]
    with Path("scripted-stderr.log").open("x") as stderr:
        client = benchmark.MCP([config["command"], *config["args"]], os.environ.copy(), Path.cwd(), stderr, 10)
        try:
            client.request("initialize", {"protocolVersion":"2025-11-25", "capabilities":{},
                "clientInfo":{"name":"scripted-factor-fixture", "version":"1"}})
            client.send({"jsonrpc":"2.0", "method":"notifications/initialized"})
            client.request("tools/list", {})
            first = "search_semantic" if policy == "semantic_first" else "search_exact"
            actions = [(first, {"query":"delay"}), ("read_source", {"path":"policy.py", "start_line":1, "end_line":1})]
            response = None
            for i, (name, arguments) in enumerate(actions):
                print(json.dumps({"type":"assistant", "message":{"content":[{"type":"tool_use", "id":str(i),
                      "name":"mcp__retrieval__" + name, "input":arguments}]}}), flush=True)
                response = client.request("tools/call", {"name":name, "arguments":arguments})
                print(json.dumps({"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":str(i), "content":response}]}}), flush=True)
                if response.get("isError"):
                    raise ValueError("scripted MCP call failed")
            content = response.get("structuredContent") or json.loads(response["content"][0]["text"])
            # Derive the fixture answer from returned source, never execute corpus code.
            line = content["lines"][0]["text"]
            value = ast.literal_eval(line.split("=", 1)[1].strip())
            print(json.dumps({"type":"result", "is_error":False, "result":json.dumps({"answer":value}), "test_only":True}), flush=True)
        finally:
            client.close()


def run(args):
    if args.client == "claude" and (not args.allow_model_usage or not args.model):
        raise ValueError("Claude requires --allow-model-usage and an explicit --model; nothing launched")
    if not math.isfinite(args.max_budget_usd) or min(args.timeout, args.tool_timeout, args.max_calls, args.max_bytes, args.max_budget_usd) <= 0:
        raise ValueError("positive limits required")
    root, output, server = args.root.resolve(strict=True), args.output.resolve(), args.server.resolve(strict=True)
    if output.is_relative_to(root) or args.questions.resolve().is_relative_to(root):
        raise ValueError("output and gold questions must be outside the corpus")
    # A debug server inflates indexing and query latency several-fold; never measure with one by accident.
    build = server.parent.name
    if build != "release" and not args.allow_debug_build:
        raise ValueError(f"server built at {build}; pass --allow-debug-build to measure a non-release build")
    cache = args.semantic_cache.resolve() if args.semantic_cache else None
    if cache and (not cache.is_absolute() or not cache.is_dir()):
        raise ValueError("shared semantic cache must be an existing absolute directory")
    tasks = json.loads(args.questions.read_text())
    if args.client == "scripted" and (root != Path(__file__).with_name("sample_repo").resolve() or len(tasks) != 1 or tasks[0]["id"] != "delay-cap"):
        raise ValueError("scripted mode is only a plumbing test of the bundled delay-cap fixture")
    for task in tasks:
        if not benchmark.grade_answer(task, json.dumps(task["expected_json"]), GRADING)["correct"]:
            raise ValueError("invalid typed gold")
    keep = [c.strip() for c in args.cells.split(",") if c.strip()] if args.cells else None
    schedule = plan(tasks, args.repetitions, args.seed, args.control, keep)
    semantic_cells = [c for c in schedule["conditions"] if "search_semantic" in benchmark.TOOLS[c["availability"]]]
    if semantic_cells and cache and not any(cache.iterdir()):
        raise ValueError("shared semantic cache is empty; warm it with warm_semantic.py before running")
    before = benchmark.fingerprint(root)
    manifest = {"version":"factor-runner-v1", "client":args.client, "model":args.model if args.client == "claude" else "scripted-no-inference",
        "effort":"medium" if args.client == "claude" else None, "root":str(root), "corpus":before,
        "server":str(server), "server_sha256":digest(server), "questions_sha256":digest(args.questions),
        "code_sha256":{name:digest(Path(__file__).with_name(name)) for name in
            ("factor_runner.py", "policy_gate.py", "plan_factors.py", "benchmark.py", "test_experiments.py")},
        "timeout":args.timeout, "tool_timeout":args.tool_timeout, "max_calls":args.max_calls,
        "max_bytes":args.max_bytes, "max_budget_usd":args.max_budget_usd, "repetitions":args.repetitions,
        "seed":args.seed, "semantic_command":args.semantic_command,
        "build_profile":build,
        "cache_policy":("shared warm semantic cache; lazy structural index" if cache
                        else "fresh per-attempt semantic cache; lazy structural index; no warmup"),
        "semantic_cache":str(cache) if cache else None,
        "transport":"serial policy gate for every condition", "auth":"subscription" if args.client == "claude" else None}
    if args.client == "claude" and semantic_cells and not args.semantic_command:
        raise ValueError("semantic conditions require an explicitly configured semantic backend")
    if args.client == "claude":
        manifest["client_executable"] = shutil.which("claude")
        manifest["client_version"] = subprocess.run(["claude", "--version"], capture_output=True,
            text=True, timeout=10, check=True).stdout.strip()
    output.mkdir(parents=True, exist_ok=args.resume, mode=0o700)
    with (output/".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_path = output/"manifest.json"
        if manifest_path.exists():
            if not args.resume or json.loads(manifest_path.read_text()) != manifest:
                raise ValueError("resume requires an identical pinned manifest")
            if json.loads((output/"plan.json").read_text()) != schedule:
                raise ValueError("stored plan changed")
        else:
            benchmark.write_json(manifest_path, manifest)
            benchmark.write_json(output/"plan.json", schedule)
        completed, failed, consecutive_failures = 0, 0, 0
        cells = {c["id"]:c for c in schedule["conditions"]}
        questions = {t["id"]:t for t in tasks}
        for index, trial in enumerate(schedule["trials"]):
            directory = output/f"trial-{index:04d}"
            attempts = sorted(directory.glob("attempt-[0-9][0-9][0-9][0-9]"))
            if attempts:
                record = attempts[-1]/"run.json"
                previous = json.loads(record.read_text()) if record.exists() else {"status":"interrupted_before_record"}
                if previous["status"] == "completed":
                    if not all(digest(attempts[-1]/name) == sha for name,sha in previous["artifacts_sha256"].items()):
                        raise ValueError("completed attempt artifacts changed")
                    completed += 1
                    continue
                if not args.retry_failed:
                    failed += 1
                    continue
            if benchmark.fingerprint(root) != before:
                raise ValueError("corpus changed; stopped")
            attempt = directory/f"attempt-{len(attempts)+1:04d}"
            attempt.mkdir(parents=True, mode=0o700)
            cell = cells[trial["condition"]]
            control = cell["availability"] == "N"
            command = [str(server), "--root", str(root), "--profile", cell["availability"],
                "--log-file", str(attempt/"server.jsonl"), "--run-id", f"trial-{index:04d}-{len(attempts)+1}",
                "--timeout-seconds", str(args.tool_timeout)]
            if "search_semantic" in benchmark.TOOLS[cell["availability"]]:
                semantic = args.semantic_command if args.client == "claude" else [sys.executable,
                    str(Path(__file__).with_name("test_experiments.py")), "--fake-semantic"]
                command += ["--semantic-command", json.dumps(semantic)]
            gate_config = {"profile":cell["availability"], "policy":cell["routing"], "max_calls":args.max_calls,
                "max_bytes":args.max_bytes, "gate_log":str(attempt/"policy.jsonl"), "stderr":str(attempt/"server-stderr.log"),
                "command":command, "root":str(root), "timeout":args.tool_timeout}
            if control:
                benchmark.write_json(attempt/"mcp.json", {"mcpServers":{}})
            else:
                benchmark.write_json(attempt/"gate.json", gate_config)
                benchmark.write_json(attempt/"mcp.json", {"mcpServers":{"retrieval":{"command":sys.executable,
                    "args":[str(Path(__file__).with_name("policy_gate.py")), "--config", str(attempt/"gate.json")]}}})
            (attempt/"prompt.txt").write_text(trial["prompt"])
            agent = ([sys.executable, str(Path(__file__).resolve()), "--scripted-agent",
                      "none" if control else str(attempt/"mcp.json"), cell["routing"]]
                if args.client == "scripted" else benchmark.agent_command(SimpleNamespace(client="claude", claude_auth="subscription",
                    model=args.model, max_budget_usd=args.max_budget_usd), attempt, cell["availability"]))
            state = {**trial, "status":"running", "condition_factors":cell, "attempt":len(attempts)+1,
                     "test_only":args.client == "scripted", "agent_command":agent}
            benchmark.write_json(attempt/"run.json", state)
            start = time.monotonic()
            env = dict(os.environ, RETRIEVAL_SEMANTIC_CACHE_DIR=str(cache or attempt/"semantic-cache"))
            try:
                with (attempt/"transcript.jsonl").open("x") as stdout, (attempt/"client-stderr.log").open("x") as stderr:
                    process = subprocess.Popen(agent, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                        cwd=attempt, env=env, text=True, start_new_session=True)
                    try:
                        process.communicate(trial["prompt"], timeout=args.timeout)
                    finally:
                        benchmark.stop_process(process)
                outcome = benchmark.transcript_outcome(attempt/"transcript.jsonl")
                state.update(outcome)
                # The control declares no servers, so an empty failure list is right for every cell.
                healthy = not outcome["mcp_failures"]
                state["status"] = ("completed" if process.returncode == 0 and outcome["answer"] is not None
                                   and not outcome["client_error"] and healthy else "failed")
                if not healthy:
                    state["error"] = f"retrieval server unavailable: {outcome['mcp_failures']}"
                # Freeze a conservative primary rule; supplementary prose review is separate.
                scored = benchmark.grade_answer(questions[trial["task_id"]], outcome["answer"], GRADING)
                state["grading"] = scored["grading"]
                state["payload_matches"] = scored["correct"]
                state["format_correct"] = scored["format_correct"]
                state["correct"] = scored["correct"] and scored["format_correct"]
            except Exception as error:
                state.update(status="timeout" if isinstance(error, subprocess.TimeoutExpired) else "failed", error=str(error))
            finally:
                state["wall_time_ms"] = (time.monotonic()-start)*1000
                state["repository_unchanged"] = benchmark.fingerprint(root) == before
                events = [json.loads(line) for line in (attempt/"policy.jsonl").read_text().splitlines()] if (attempt/"policy.jsonl").exists() else []
                state["attempted_calls"] = len(events)
                state["first_attempt"] = events[0]["tool"] if events else None
                state["policy_violations"] = sum(e["reason"] == "first_tool_policy_violation" for e in events)
                required = {"free":None, "lexical_first":"search_exact", "semantic_first":"search_semantic"}[cell["routing"]]
                state["policy_adherent"] = (required is None or state["first_attempt"] == required) and not state["policy_violations"]
                # A control trial that reached any retrieval tool is contaminated, not merely adherent.
                state["control_without_retrieval"] = control and state["attempted_calls"] == 0 and not state["unexpected_tools"] if control else None
                state["budget_exhausted"] = any(e["reason"] in ("budget_exhausted", "response_budget_exhausted") for e in events)
                state["artifacts_sha256"] = {name:digest(attempt/name) for name in ("prompt.txt", "transcript.jsonl", "policy.jsonl", "server.jsonl") if (attempt/name).exists()}
                benchmark.write_json(attempt/"run.json", state)
            completed += state["status"] == "completed"
            failed += state["status"] != "completed"
            consecutive_failures = consecutive_failures + 1 if state["status"] != "completed" else 0
            if not state["repository_unchanged"] or consecutive_failures >= 3:
                break
        return {"completed":completed, "failed":failed, "planned":len(schedule["trials"]), "test_only":args.client == "scripted"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=base/"sample_repo")
    parser.add_argument("--questions", type=Path, default=base/"factor_smoke.json")
    parser.add_argument("--server", type=Path, default=base.parent/"target/debug/retrieval-mcp")
    parser.add_argument("--client", choices=("scripted", "claude"), default="scripted")
    parser.add_argument("--allow-model-usage", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--semantic-command", type=benchmark.command_array)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--tool-timeout", type=int, default=60)
    parser.add_argument("--semantic-cache", type=Path, help="shared warm cache directory for semantic cells")
    parser.add_argument("--allow-debug-build", action="store_true")
    parser.add_argument("--control", action="store_true", help="include the tool-free control cell")
    parser.add_argument("--cells", help="comma-separated condition IDs to keep")
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--max-bytes", type=int, default=200000)
    parser.add_argument("--max-budget-usd", type=float, default=1)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(result))
    return int(result["failed"] > 0)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--scripted-agent"]:
        sys.stdin.read()
        scripted(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(main())
