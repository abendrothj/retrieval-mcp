#!/usr/bin/env python3
"""Study A.3: when a structural action fails, which part of the navigation was wrong?

Study A.2 showed the model reaches for the right structural primitive and still misses. That can
fail in four distinct places, and "retrieval was bad" is none of them:

    unavailable   no correct seed appeared in the evidence the model saw
    unchosen      a correct seed was visible and the model anchored somewhere else
    wrong_level   the seed is real and adjacent, but the gold sits at a different graph node
    wrong_reach   the seed reaches the gold, but not in the direction or depth that was used

A correct seed is one from which the gold is reachable within the tool's own traversal, so the
classification is made by the same index the model queries, not by a hand-drawn call graph. No model
calls: the decisions are read from the frozen A.2 artifact and everything else is deterministic.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile

import benchmark
import comparison_runner
import study_a

STRUCTURAL = ("find_symbol", "find_callers", "trace_dependencies")
MAX_DEPTH = 5


def call(client, name, arguments):
    result = client.request("tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        return []
    return (result.get("structuredContent") or {}).get("results", [])


def neighbourhood(client, name, depth):
    """Every symbol name within `depth` call hops of `name`, in either direction."""
    found = set()
    for direction in ("callers", "callees"):
        for edge in call(client, "trace_dependencies",
                         {"name": name, "direction": direction, "depth": depth, "limit": 100}):
            found.add((edge.get("path"), edge.get("caller")))
            found.add((edge.get("path"), edge.get("callee")))
    return {pair for pair in found if pair[0] and pair[1]}


def reaches(client, seed, relevant, max_depth):
    """Smallest traversal depth at which the seed reaches any relevant symbol, or None."""
    for depth in range(1, max_depth + 1):
        if neighbourhood(client, seed, depth) & relevant:
            return depth
    return None


def run(args):
    questions = {task["id"]: task
                 for task in json.loads(args.questions.read_text(encoding="utf-8"))}
    decisions = json.loads(args.decisions.read_text(encoding="utf-8"))["cells"]
    entries = json.loads(args.reformulations.read_text(encoding="utf-8"))["reformulations"]
    before = comparison_runner.source_fingerprint(args.root)
    command = [str(args.server), "--root", str(args.root), "--profile", "D",
               "--ranker", "lexical", "--timeout-seconds", str(args.timeout)]
    findings = {}
    with tempfile.TemporaryFile(mode="w+") as stderr:
        client = benchmark.MCP(command, dict(os.environ), args.root, stderr, args.timeout + 60)
        try:
            client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "study-a3", "version": "1"}})
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            for task_id, decision in decisions.items():
                if decision["action"] not in STRUCTURAL or decision["stage2_rank"]:
                    continue
                task = questions[task_id]
                relevant = study_a.relevant_symbols(task)
                seed = decision["argument"]
                # What the model could see when it chose: the stage-1 rows, recomputed.
                rewrite = " ".join([*entries[task_id]["concepts"],
                                    *entries[task_id]["candidate_identifiers"],
                                    *entries[task_id]["terms"]])
                visible = {study_a.row_identity(row)
                           for row in call(client, "search_concept",
                                           {"query": rewrite, "limit": args.limit})}
                # Seeds that would have worked: one hop from any relevant symbol.
                workable = set()
                for path, name in relevant:
                    workable |= {pair[1] for pair in neighbourhood(client, name, 1)}
                    workable.add(name)
                seed_defined = bool(call(client, "find_symbol", {"name": seed, "limit": 5}))
                depth = reaches(client, seed, relevant, MAX_DEPTH) if seed_defined else None
                visible_workable = sorted({name for _, name in visible if name in workable})
                if depth is not None:
                    verdict = "wrong_reach"
                elif not seed_defined:
                    verdict = "unavailable" if not visible_workable else "unchosen"
                elif visible_workable:
                    verdict = "unchosen"
                else:
                    verdict = "wrong_level" if visible else "unavailable"
                findings[task_id] = {
                    "category": task["category"],
                    "action": decision["action"],
                    "seed": seed,
                    "seed_is_indexed_definition": seed_defined,
                    "gold_reachable_from_seed_at_depth": depth,
                    "workable_seeds_visible_in_evidence": visible_workable,
                    "relevant": sorted("::".join(pair) for pair in relevant),
                    "verdict": verdict,
                }
        finally:
            client.close()
    if comparison_runner.source_fingerprint(args.root) != before:
        raise RuntimeError("the corpus changed while it was being analysed; results are void")
    return {
        "version": "study-a3-v1",
        "max_depth_probed": MAX_DEPTH,
        "structural_failures": len(findings),
        "verdicts": {verdict: sum(1 for f in findings.values() if f["verdict"] == verdict)
                     for verdict in ("unavailable", "unchosen", "wrong_level", "wrong_reach")},
        "corpus": before,
        "findings": findings,
        "limitations": "Reachability is judged by the same syntactic index the model queries, so a "
                       "binding it cannot resolve is invisible here too. Seeds are classified one "
                       "hop from gold; a longer legitimate route would read as unavailable.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True, help="a study_a2 output file")
    parser.add_argument("--reformulations", type=Path, required=True)
    parser.add_argument("--server", type=Path, default=base.parent / "target/release/retrieval-mcp")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.root = args.root.resolve(strict=True)
    result = run(args)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, result)
    print(json.dumps({"verdicts": result["verdicts"],
                      "findings": {task_id: {key: finding[key] for key in
                                             ("category", "action", "seed", "verdict",
                                              "workable_seeds_visible_in_evidence")}
                                   for task_id, finding in result["findings"].items()}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
