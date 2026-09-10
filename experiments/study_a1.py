#!/usr/bin/env python3
"""Study A.1: does rewriting the question into codebase vocabulary rescue single-shot retrieval?

Factorial over query form and ranker, with corpus, chunks, gold, and k held fixed:

    {original, reformulated} x {lexical, semantic, hybrid}

Three outcomes are distinguishable. If only lexical improves, the model is acting as a
semantic-to-lexical translator. If both improve, it is doing general query expansion. If neither
moves, single-shot retrieval is the wrong frame and the agent's advantage lives in iteration
against retrieved evidence, not in a better first query.

Overlap is reported conditioned on success. Two rankers that both miss are not exploring
complementary evidence spaces; they are failing in different neighbourhoods.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

import benchmark
import comparison_runner
import study_a

FORMS = ("original", "reformulated")


def reformulated_query(entry):
    """Frozen construction rule: concepts, then identifiers, then literal terms."""
    return " ".join([*entry["concepts"], *entry["candidate_identifiers"], *entry["terms"]])


def conditioned_overlap(left_cells, right_cells, cutoff):
    """Jaccard split by which ranker actually found the target."""
    buckets = {"both": [], "left_only": [], "right_only": [], "neither": []}
    for task_id, left in left_cells.items():
        right = right_cells[task_id]
        hit = lambda cell: bool(cell["rank"]) and cell["rank"] <= cutoff
        name = ("both" if hit(left) and hit(right)
                else "left_only" if hit(left)
                else "right_only" if hit(right) else "neither")
        a, b = set(left["ranked"][:cutoff]), set(right["ranked"][:cutoff])
        buckets[name].append(len(a & b) / len(a | b) if a | b else 0.0)
    return {name: {"n": len(values),
                   "jaccard": round(statistics.fmean(values), 3) if values else None}
            for name, values in buckets.items()}


def run(args):
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    graded = [task for task in questions if study_a.relevant_symbols(task)]
    artifact = json.loads(args.reformulations.read_text(encoding="utf-8"))
    entries = artifact["reformulations"]
    missing = [task["id"] for task in graded if task["id"] not in entries]
    if missing:
        raise ValueError(f"no reformulation for {missing}")
    cache = args.cache.resolve()
    if cache == args.root or cache.is_relative_to(args.root):
        raise ValueError("the vector cache must live outside the corpus")
    cache.mkdir(parents=True, exist_ok=True)
    before = comparison_runner.source_fingerprint(args.root)
    variants = {
        "original": graded,
        "reformulated": [{**task, "question": reformulated_query(entries[task["id"]])}
                         for task in graded],
    }
    conditions = {}
    for form in FORMS:
        for ranker in args.rankers:
            rows, cold, latencies = study_a.query_all(
                args.server, args.root, ranker, args.semantic_command, variants[form],
                args.limit, args.timeout, cache)
            cells = {}
            for task in graded:
                relevant = study_a.relevant_symbols(task)
                ranked = rows[task["id"]]
                identities = [study_a.row_identity(row) for row in ranked]
                cells[task["id"]] = {
                    "category": task["category"],
                    "rank": study_a.scored(ranked, relevant),
                    "ranked": ["::".join(part for part in identity if part)
                               for identity in identities],
                }
            conditions[f"{form}/{ranker}"] = {
                "effectiveness": study_a.summarize(cells),
                "warm_query_ms_median": round(statistics.median(latencies.values()), 1),
                "cold_first_query_ms": round(cold, 1),
                "cells": cells,
            }
    if comparison_runner.source_fingerprint(args.root) != before:
        raise RuntimeError("the corpus changed while it was being ranked; results are void")
    overlap = {}
    for form in FORMS:
        left, right = f"{form}/lexical", f"{form}/semantic"
        if left in conditions and right in conditions:
            overlap[form] = {f"@{cutoff}": conditioned_overlap(
                conditions[left]["cells"], conditions[right]["cells"], cutoff)
                for cutoff in study_a.CUTOFFS}
    return {
        "version": "study-a1-v1",
        "questions_graded": len(graded),
        "limit": args.limit,
        "corpus": before,
        "reformulation_model": artifact["model"],
        "reformulation_variant": artifact["variant"],
        "reformulation_cost_usd": artifact["cost_usd"],
        "query_construction": "concepts, then candidate_identifiers, then terms, space joined",
        "conditions": conditions,
        "overlap_by_success": overlap,
        "limitations": "The rewrite never saw the corpus, the index, or any retrieval result, so "
                       "this measures static query formation only. It cannot speak to iterative, "
                       "evidence-conditioned search, which is what an agent loop actually does.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--reformulations", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--server", type=Path, default=base.parent / "target/release/retrieval-mcp")
    parser.add_argument("--semantic-command", type=benchmark.command_array, required=True)
    parser.add_argument("--rankers", nargs="+", default=list(study_a.RANKERS),
                        choices=study_a.RANKERS)
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
    print(json.dumps({name: {"mrr": condition["effectiveness"]["mrr"],
                             **{f"recall@{k}": condition["effectiveness"][f"recall@{k}"]
                                for k in study_a.CUTOFFS}}
                      for name, condition in result["conditions"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
