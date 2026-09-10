#!/usr/bin/env python3
"""Run synthetic syntax/structure probes through the policy gate; explicit model-use gate.

Scripted mode exercises plumbing with deterministic fixture actions and measures no model
behaviour. Real probes require separately approved model usage.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import shutil
import sys
import time
from types import SimpleNamespace

import benchmark
from benchmark import TOOLS
from plan_factors import BASE, PRIMER, ROUTING
from score_competency import score


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hits(results, path="tokens.py"):
    return [hit["line"] for page in results for hit in page["results"] if hit.get("path") == path]


# Deterministic derivations from delivered MCP results; gold is never given to the agent.
FIXTURES = {
    "literal-pipe": lambda results: hits(results)[0],
    "regex-alternation": lambda results: sorted(hits(results)),
    "regex-literal-pipe": lambda results: sorted(hits(results)),
    "paginate-entries": lambda results: sorted(set(hits(results))),
    "bounded-read": lambda results: ast.literal_eval(results[0]["lines"][0]["text"].split("=", 1)[1].strip()),
}


def scripted(config_path, task_json):
    """Fixture agent: performs the probe's reference actions, derives the answer from responses."""
    task = json.loads(task_json)
    config = json.loads(Path(config_path).read_text())["mcpServers"]["retrieval"]
    with Path("scripted-stderr.log").open("x") as stderr:
        client = benchmark.MCP([config["command"], *config["args"]], os.environ.copy(), Path.cwd(), stderr, 10)
        try:
            client.request("initialize", {"protocolVersion":"2025-11-25", "capabilities":{},
                "clientInfo":{"name":"scripted-competency-fixture", "version":"1"}})
            client.send({"jsonrpc":"2.0", "method":"notifications/initialized"})
            client.request("tools/list", {})
            results = []
            for i, action in enumerate(task["reference_actions"]):
                print(json.dumps({"type":"assistant", "message":{"content":[{"type":"tool_use", "id":str(i),
                      "name":"mcp__retrieval__" + action["name"], "input":action["arguments"]}]}}), flush=True)
                response = client.request("tools/call", action)
                print(json.dumps({"type":"user", "message":{"content":[{"type":"tool_result",
                      "tool_use_id":str(i), "content":response}]}}), flush=True)
                if response.get("isError"):
                    raise ValueError("scripted MCP call failed")
                results.append(response.get("structuredContent") or json.loads(response["content"][0]["text"]))
            answer = FIXTURES[task["id"]](results)
            print(json.dumps({"type":"result", "is_error":False,
                  "result":json.dumps({"answer":answer}), "test_only":True}), flush=True)
        finally:
            client.close()


def calls_of(attempt):
    """Join gate attempts with their delivered responses; rejected attempts are kept."""
    events = [json.loads(line) for line in (attempt/"policy.jsonl").read_text().splitlines()] \
        if (attempt/"policy.jsonl").exists() else []
    bodies = {}
    if (attempt/"responses.jsonl").exists():
        for line in (attempt/"responses.jsonl").read_text().splitlines():
            record = json.loads(line)
            bodies[record["attempt"]] = record["result"]
    return events, [{"name":event["tool"], "arguments":event["arguments"] or {},
                     "result":bodies.get(event["attempt"], {"isError":True}),
                     "forwarded":event["forwarded"], "reason":event["reason"]} for event in events]


def run(args):
    if args.client == "claude" and (not args.allow_model_usage or not args.model):
        raise ValueError("Claude requires --allow-model-usage and an explicit --model; nothing launched")
    if min(args.timeout, args.tool_timeout, args.max_calls, args.max_bytes, args.max_budget_usd) <= 0:
        raise ValueError("positive limits required")
    levels = [level.strip() for level in args.help_levels.split(",") if level.strip()]
    if not levels or any(level not in ("baseline", "primer") for level in levels):
        raise ValueError("help levels must be baseline and/or primer")
    root, output, server = args.root.resolve(strict=True), args.output.resolve(), args.server.resolve(strict=True)
    if output.is_relative_to(root) or args.questions.resolve().is_relative_to(root):
        raise ValueError("output and gold questions must be outside the corpus")
    tasks = json.loads(args.questions.read_text())
    if args.ids:
        wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
        tasks = [t for t in tasks if t["id"] in wanted]
        if len(tasks) != len(set(wanted)):
            raise ValueError("unknown probe ID requested")
    if not tasks:
        raise ValueError("no probes selected")
    for task in tasks:
        if not benchmark.grade_answer(task, json.dumps(task["expected_json"]))["correct"]:
            raise ValueError("invalid typed gold")
        # Never run a probe whose evidence needs a tool this profile omits.
        missing = sorted({a["name"] for a in task["reference_actions"]} - set(TOOLS[args.profile]))
        if missing:
            raise ValueError(f"probe {task['id']} needs {missing}; unavailable in profile {args.profile}")
        if args.client == "scripted" and task["id"] not in FIXTURES:
            raise ValueError(f"probe {task['id']} has no deterministic fixture agent; it needs an authorized model trial")
    trials = [{"task_id":task["id"], "syntax_help":level,
               "prompt":BASE + ROUTING["free"] + (PRIMER if level == "primer" else "") + "\n" + task["question"]}
              for task in tasks for level in levels]
    for trial in trials:
        trial["prompt_sha256"] = hashlib.sha256(trial["prompt"].encode()).hexdigest()
    random.Random(args.seed).shuffle(trials)
    before = benchmark.fingerprint(root)
    manifest = {"version":"competency-runner-v1", "client":args.client,
        "model":args.model if args.client == "claude" else "scripted-no-inference",
        "effort":"medium" if args.client == "claude" else None, "profile":args.profile,
        "routing":"free", "help_levels":levels, "root":str(root), "corpus":before,
        "server":str(server), "server_sha256":digest(server), "questions_sha256":digest(args.questions),
        "code_sha256":{name:digest(Path(__file__).with_name(name)) for name in
            ("competency_runner.py", "score_competency.py", "policy_gate.py", "plan_factors.py", "benchmark.py")},
        "timeout":args.timeout, "tool_timeout":args.tool_timeout, "max_calls":args.max_calls,
        "max_bytes":args.max_bytes, "max_budget_usd":args.max_budget_usd, "seed":args.seed,
        "grading":"competency-strict-v1", "probes":[t["id"] for t in tasks],
        "cache_policy":"fresh per-attempt semantic cache; lazy structural index; no warmup",
        "limitations":"Probes measure retrieval-argument behaviour on synthetic files, not repository skill. "
                      "Scripted runs replay fixed actions and measure no model behaviour or primer effect."}
    if args.client == "claude":
        manifest["client_executable"] = shutil.which("claude")
        manifest["client_version"] = subprocess.run(["claude", "--version"], capture_output=True,
            text=True, timeout=10, check=True).stdout.strip()
    output.mkdir(parents=True, mode=0o700)
    benchmark.write_json(output/"manifest.json", manifest)
    benchmark.write_json(output/"plan.json", trials)
    questions = {t["id"]:t for t in tasks}
    completed = failed = passes = 0
    for index, trial in enumerate(trials):
        if benchmark.fingerprint(root) != before:
            raise ValueError("corpus changed; stopped")
        attempt = output/f"probe-{index:04d}"
        attempt.mkdir(mode=0o700)
        command = [str(server), "--root", str(root), "--profile", args.profile,
            "--log-file", str(attempt/"server.jsonl"), "--run-id", f"probe-{index:04d}",
            "--timeout-seconds", str(args.tool_timeout)]
        semantic = args.semantic_command if args.client == "claude" else [sys.executable,
            str(Path(__file__).with_name("test_experiments.py")), "--fake-semantic"]
        if "search_concept" in TOOLS[args.profile]:
            if args.client == "claude" and not args.semantic_command:
                raise ValueError("profiles with search_concept require an explicit semantic backend")
            command += ["--semantic-command", json.dumps(semantic)]
        benchmark.write_json(attempt/"gate.json", {"profile":args.profile, "policy":"free",
            "max_calls":args.max_calls, "max_bytes":args.max_bytes, "gate_log":str(attempt/"policy.jsonl"),
            "response_log":str(attempt/"responses.jsonl"), "stderr":str(attempt/"server-stderr.log"),
            "command":command, "root":str(root), "timeout":args.tool_timeout})
        benchmark.write_json(attempt/"mcp.json", {"mcpServers":{"retrieval":{"command":sys.executable,
            "args":[str(Path(__file__).with_name("policy_gate.py")), "--config", str(attempt/"gate.json")]}}})
        (attempt/"prompt.txt").write_text(trial["prompt"])
        agent = ([sys.executable, str(Path(__file__).resolve()), "--scripted-agent", str(attempt/"mcp.json"),
                  json.dumps(questions[trial["task_id"]])] if args.client == "scripted"
                 else benchmark.agent_command(SimpleNamespace(client="claude", claude_auth="subscription",
                     model=args.model, max_budget_usd=args.max_budget_usd), attempt, args.profile))
        state = {**trial, "status":"running", "profile":args.profile, "attempt":1,
                 "test_only":args.client == "scripted",
                 "fixture_ignores_prompt":args.client == "scripted", "agent_command":agent}
        benchmark.write_json(attempt/"run.json", state)
        start = time.monotonic()
        env = dict(os.environ, RETRIEVAL_SEMANTIC_CACHE_DIR=str(attempt/"semantic-cache"))
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
            state["status"] = "completed" if process.returncode == 0 and outcome["answer"] is not None \
                and not outcome["client_error"] else "failed"
        except Exception as error:
            state.update(status="timeout" if isinstance(error, subprocess.TimeoutExpired) else "failed", error=str(error))
        finally:
            state["wall_time_ms"] = (time.monotonic()-start)*1000
            state["repository_unchanged"] = benchmark.fingerprint(root) == before
            events, calls = calls_of(attempt)
            state["attempted_calls"] = len(events)
            state["first_attempt"] = events[0]["tool"] if events else None
            state["budget_exhausted"] = any(e["reason"] in ("budget_exhausted", "response_budget_exhausted") for e in events)
            state["scored"] = score(questions[trial["task_id"]], state.get("answer"), calls)
            state["calls"] = [{k:c[k] for k in ("name", "arguments", "forwarded", "reason")} for c in calls]
            state["artifacts_sha256"] = {name:digest(attempt/name) for name in
                ("prompt.txt", "transcript.jsonl", "policy.jsonl", "responses.jsonl", "server.jsonl")
                if (attempt/name).exists()}
            benchmark.write_json(attempt/"run.json", state)
        completed += state["status"] == "completed"
        failed += state["status"] != "completed"
        passes += state["status"] == "completed" and state["scored"]["correct"]
        if not state["repository_unchanged"]:
            break
    summary = {"version":"competency-summary-v1", "test_only":args.client == "scripted",
               "planned":len(trials), "completed":completed, "failed":failed, "probe_passes":passes}
    benchmark.write_json(output/"summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=base/"competency_repo")
    parser.add_argument("--questions", type=Path, default=base/"competency_questions.json")
    parser.add_argument("--server", type=Path, default=base.parent/"target/debug/retrieval-mcp")
    parser.add_argument("--profile", choices=tuple(TOOLS), default="B")
    parser.add_argument("--help-levels", default="baseline,primer")
    parser.add_argument("--ids", help="comma-separated probe IDs; default all")
    parser.add_argument("--client", choices=("scripted", "claude"), default="scripted")
    parser.add_argument("--allow-model-usage", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--semantic-command", type=benchmark.command_array)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--tool-timeout", type=int, default=10)
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--max-bytes", type=int, default=200000)
    parser.add_argument("--max-budget-usd", type=float, default=1)
    parser.add_argument("--seed", type=int, default=42)
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
