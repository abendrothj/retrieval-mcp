#!/usr/bin/env python3
"""Rank the same questions with each ranker and score the candidate set. No agent, no model calls.

Isolation is the point: identical corpus, identical definition-shaped chunks, identical row schema
and filters, one variable — how `search_concept` is answered. Effectiveness (recall@k, MRR) and
mechanical economics (cold build, query latency, backend requirement) are reported separately,
because a ranker that ties while removing a service is not merely equal.

Gold is a set of acceptable symbols per question, not one symbol: several definitions can carry the
evidence. Scoring compares symbol identity, so returning the right 2000-line file is not a hit.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import statistics
import tempfile
import time

import benchmark

RANKERS = ("lexical", "semantic", "hybrid")
CUTOFFS = (1, 3, 5, 10)


def gold_symbols(task):
    """Every acceptable (path, name) pair mentioned by a typed gold answer."""
    found = set()
    def walk(value):
        if isinstance(value, str):
            if "::" in value and any(value.split("::")[0].endswith(ext) for ext in (".rs", ".py")):
                parts = value.split("::")
                found.add((parts[0], parts[-1]))
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
    walk(task["expected_json"]["answer"])
    return found


def row_identity(row):
    symbol = row.get("symbol") or {}
    if symbol.get("path") and symbol.get("name"):
        return (symbol["path"], symbol["name"])
    return (row.get("path"), None)


def scored(ranked, gold):
    """Rank of the first gold symbol, 1-based, or None."""
    for position, row in enumerate(ranked, 1):
        if row_identity(row) in gold:
            return position
    return None


def query_all(server, root, ranker, semantic_command, questions, limit, timeout, cache):
    command = [str(server), "--root", str(root), "--profile", "D", "--ranker", ranker,
               "--timeout-seconds", str(timeout)]
    if ranker != "lexical":
        command += ["--semantic-command", json.dumps(semantic_command)]
    rows, latencies = {}, {}
    with tempfile.TemporaryFile(mode="w+") as stderr:
        # Never let an adapter write its vectors into the corpus under test.
        environment = dict(os.environ, RETRIEVAL_SEMANTIC_CACHE_DIR=str(cache))
        client = benchmark.MCP(command, environment, root, stderr, timeout + 60)
        try:
            client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "study-a", "version": "1"}})
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            cold = None
            for task in questions:
                started = time.monotonic()
                result = client.request("tools/call", {"name": "search_concept", "arguments": {
                    "query": task["question"], "limit": limit}})
                elapsed = (time.monotonic() - started) * 1000
                if result.get("isError"):
                    raise RuntimeError(f"{ranker} failed on {task['id']}: {result}")
                if cold is None:
                    cold = elapsed  # First call pays index or embedding warm-up.
                else:
                    latencies[task["id"]] = elapsed
                rows[task["id"]] = result["structuredContent"]["results"]
        finally:
            client.close()
    return rows, cold, latencies


def report(args):
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    graded = [task for task in questions if gold_symbols(task)]
    conditions = {}
    for ranker in args.rankers:
        rows, cold, latencies = query_all(args.server, args.root, ranker, args.semantic_command,
                                          graded, args.limit, args.timeout, args.cache)
        cells = {}
        for task in graded:
            gold = gold_symbols(task)
            ranked = rows[task["id"]]
            rank = scored(ranked, gold)
            cells[task["id"]] = {
                "category": task["category"],
                "gold": sorted("::".join(pair) for pair in gold),
                "rank": rank,
                "returned": len(ranked),
                "bytes": len(json.dumps(ranked, ensure_ascii=False).encode()),
                "ranked": ["::".join(part for part in row_identity(row) if part)
                           for row in ranked],
            }
        conditions[ranker] = {
            "effectiveness": summarize(cells),
            "economics": {
                "cold_first_query_ms": round(cold, 1),
                "warm_query_ms_median": round(statistics.median(latencies.values()), 1),
                "warm_query_ms_max": round(max(latencies.values()), 1),
                "bytes_per_query_mean": round(statistics.fmean(
                    cell["bytes"] for cell in cells.values())),
                "external_service_required": ranker != "lexical",
            },
            "cells": cells,
        }
    return {
        "version": "study-a-v1",
        "questions_graded": len(graded),
        "questions_skipped": [task["id"] for task in questions if not gold_symbols(task)],
        "limit": args.limit,
        "conditions": conditions,
        "overlap": overlap(conditions),
        "limitations": "One corpus and one question set; gold is symbol-level and set-valued. "
                       "Queries are the raw question text, so this understates lexical ranking as "
                       "an agent uses it: a model rewrites descriptions into candidate identifiers "
                       "before searching. Deterministic, so no repetitions are run.",
    }


def summarize(cells):
    ranks = [cell["rank"] for cell in cells.values()]
    result = {"n": len(ranks),
              "mrr": round(statistics.fmean(1 / rank if rank else 0.0 for rank in ranks), 4)}
    for cutoff in CUTOFFS:
        result[f"recall@{cutoff}"] = sum(1 for rank in ranks if rank and rank <= cutoff)
    categories = {}
    for cell in cells.values():
        bucket = categories.setdefault(cell["category"], [])
        bucket.append(cell["rank"])
    result["by_category"] = {
        name: {"n": len(bucket),
               **{f"recall@{cutoff}": sum(1 for rank in bucket if rank and rank <= cutoff)
                  for cutoff in CUTOFFS}}
        for name, bucket in sorted(categories.items())}
    return result


def overlap(conditions):
    """Candidate-set agreement: recall says who wins, overlap says whether fusion can help."""
    if not {"lexical", "semantic"} <= set(conditions):
        return None
    result = {}
    for cutoff in CUTOFFS:
        shared, union, both_relevant = 0, 0, Counter()
        for task_id, left in conditions["lexical"]["cells"].items():
            right = conditions["semantic"]["cells"][task_id]
            a, b = set(left["ranked"][:cutoff]), set(right["ranked"][:cutoff])
            shared += len(a & b)
            union += len(a | b)
            if left["rank"] and left["rank"] <= cutoff:
                both_relevant["lexical"] += 1
            if right["rank"] and right["rank"] <= cutoff:
                both_relevant["semantic"] += 1
        result[f"@{cutoff}"] = {"jaccard": round(shared / union, 3) if union else None,
                                "shared_rows": shared,
                                "lexical_hits": both_relevant["lexical"],
                                "semantic_hits": both_relevant["semantic"]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--questions", type=Path, default=base / "comparison_questions.json")
    parser.add_argument("--server", type=Path, default=base.parent / "target/release/retrieval-mcp")
    parser.add_argument("--semantic-command", type=benchmark.command_array, required=True)
    parser.add_argument("--rankers", nargs="+", default=list(RANKERS), choices=RANKERS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--cache", type=Path, required=True,
                        help="semantic vector cache directory; must be outside the corpus")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.root = args.root.resolve(strict=True)
    result = report(args)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, result)
    print(json.dumps({ranker: {"effectiveness": condition["effectiveness"],
                               "economics": condition["economics"]}
                      for ranker, condition in result["conditions"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
