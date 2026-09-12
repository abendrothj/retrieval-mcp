#!/usr/bin/env python3
"""Execute authored tool paths and prove that each question's complete gold is reachable."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import tempfile

import benchmark
import quality_pass

SUPPORTED = {
    "search_exact", "read_source", "inspect_symbol", "find_symbol",
    "find_callers", "trace_dependencies", "search_concept",
}
SEED_TOOLS = {"search_exact", "find_symbol", "search_concept"}


def marker_present(value, marker):
    """Return whether one structured result carries a path-qualified marker."""
    if "::" not in marker:
        return marker in json.dumps(value, ensure_ascii=False)
    path, leaf = marker.split("::", 1)
    leaf = leaf.split("::")[-1]

    def visit(node, inherited_path=None):
        if isinstance(node, dict):
            current_path = node.get("path") if isinstance(node.get("path"), str) else inherited_path
            if current_path == path:
                for candidate in node.values():
                    if isinstance(candidate, str) and (
                        candidate == leaf or candidate.endswith(f"::{leaf}")
                    ):
                        return True
            return any(visit(child, current_path) for child in node.values())
        if isinstance(node, list):
            return any(visit(child, inherited_path) for child in node)
        if isinstance(node, str) and inherited_path == path:
            return leaf in node
        return False

    return visit(value)


def path_problems(task):
    actions = task.get("tool_path")
    if not isinstance(actions, list) or not actions:
        return ["tool_path must be a non-empty list"]
    problems = []
    gold_leaves = {
        marker.split("::")[-1]
        for marker in quality_pass.flatten((task.get("expected_json") or {}).get("answer"))
        if isinstance(marker, str) and "::" in marker
    }
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or set(action) - {"tool", "arguments", "expect", "uses_prior"}:
            problems.append(f"tool_path[{index}] has an invalid shape")
            continue
        tool = action.get("tool")
        if tool not in SUPPORTED:
            problems.append(f"tool_path[{index}] names unsupported tool {tool!r}")
        if index == 0 and tool not in SEED_TOOLS:
            problems.append(f"tool_path[0] must retrieve an initial seed, not call {tool!r}")
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            problems.append(f"tool_path[{index}].arguments must be an object")
            arguments_text = ""
        else:
            arguments_text = json.dumps(arguments, ensure_ascii=False)
        if index == 0:
            leaked = sorted(leaf for leaf in gold_leaves if leaf in arguments_text)
            if leaked:
                problems.append(f"tool_path[0] directly queries gold identifiers: {leaked}")
        expected = action.get("expect")
        if not isinstance(expected, list) or not expected or not all(
            isinstance(marker, str) and marker for marker in expected
        ):
            problems.append(f"tool_path[{index}].expect must be a non-empty string list")
        prior = action.get("uses_prior")
        if index and (not isinstance(prior, str) or not prior):
            problems.append(f"tool_path[{index}] must name evidence consumed from an earlier step")
        elif index:
            path, _, rest = prior.partition("::")
            leaf = rest.split("::")[-1] if rest else prior
            if leaf not in arguments_text and (not rest or path not in arguments_text):
                problems.append(
                    f"tool_path[{index}] arguments do not consume prior marker {prior!r}"
                )
        if not index and prior is not None:
            problems.append("tool_path[0] cannot consume earlier evidence")
    return problems


def execute(questions, command, corpus, environment, timeout):
    findings = {}
    calls = Counter()
    stderr = tempfile.TemporaryFile(mode="w+")
    client = benchmark.MCP(command, environment, corpus, stderr, timeout)
    try:
        client.request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "tool-reachability", "version": "1"},
        })
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        advertised = {tool["name"] for tool in client.request("tools/list", {})["tools"]}
        if advertised != SUPPORTED:
            raise RuntimeError(f"server advertised {sorted(advertised)}, expected {sorted(SUPPORTED)}")

        for task in questions:
            problems = path_problems(task)
            outputs = []
            if not problems:
                for index, action in enumerate(task["tool_path"]):
                    prior = action.get("uses_prior")
                    if prior and not marker_present(outputs, prior):
                        problems.append(
                            f"tool_path[{index}] cannot derive {prior!r} from earlier output"
                        )
                        break
                    result = client.request("tools/call", {
                        "name": action["tool"], "arguments": action["arguments"],
                    })
                    calls[action["tool"]] += 1
                    if result.get("isError"):
                        problems.append(f"tool_path[{index}] returned an error")
                        break
                    structured = result.get("structuredContent") or result
                    outputs.append(structured)
                    for marker in action["expect"]:
                        if not marker_present(structured, marker):
                            problems.append(
                                f"tool_path[{index}] did not expose expected marker {marker!r}"
                            )
                for marker in quality_pass.flatten(task["expected_json"]["answer"]):
                    if isinstance(marker, str) and "::" in marker and not marker_present(outputs, marker):
                        problems.append(f"complete tool path did not expose gold marker {marker!r}")
            if problems:
                findings[task.get("id", "<unknown>")] = problems
    finally:
        client.close()
        stderr.close()
    return {
        "version": "tool-reachability-v1",
        "questions": len(questions),
        "reachable": len(questions) - len(findings),
        "tool_calls": dict(sorted(calls.items())),
        "findings": findings,
        "limitations": (
            "Proves one authored path against the pinned tool implementation. It does not prove "
            "that a model will choose that path or that syntax-only structural edges are bindings."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--semantic-command", type=benchmark.command_array, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    questions = json.loads(args.questions.resolve(strict=True).read_text(encoding="utf-8"))
    corpus = args.corpus.resolve(strict=True)
    server = args.server.resolve(strict=True)
    command = [
        str(server), "--root", str(corpus), "--profile", "D", "--semantic-command",
        json.dumps(args.semantic_command), "--timeout-seconds", str(args.timeout),
    ]
    environment = dict(os.environ)
    environment["RETRIEVAL_SEMANTIC_CACHE_DIR"] = str(args.cache.resolve())
    report = execute(questions, command, corpus, environment, args.timeout)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, report)
    print(json.dumps({key: report[key] for key in ("questions", "reachable", "tool_calls")}, indent=2))
    for task_id, problems in report["findings"].items():
        print(f"\n{task_id}")
        for problem in problems:
            print(f"  - {problem}")
    return int(report["reachable"] != report["questions"])


if __name__ == "__main__":
    raise SystemExit(main())
