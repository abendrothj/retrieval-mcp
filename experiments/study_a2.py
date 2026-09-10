#!/usr/bin/env python3
"""Study A.2: what does seeing retrieved evidence add, once the query has already been rewritten?

Three stages over one frozen gold set, corpus, and k:

    stage 0  the human question, ranked directly
    stage 1  the corpus-blind rewrite from Study A.1, ranked
    stage 2  the model sees the question, its own rewrite, and stage-1 rows, then chooses ONE next
             action from the whole tool surface, which is executed verbatim

Stage 2 is deliberately not forced to be another search. A question whose answer is a relationship
should stop being a search problem once a plausible seed symbol is on the table, and refusing the
model that choice would assume the conclusion.

Every term the model uses at stage 2 is attributed to the human question, to its own corpus-blind
rewrite, or to vocabulary it could only have learned from the retrieved rows. That is the difference
between an agent retrying and an agent acquiring the repository's own words.
"""
import argparse
import json
import os
from pathlib import Path
import re
import statistics
import tempfile

import benchmark
import comparison_runner
import make_reformulations
import study_a

DIRECTED = ("search_concept", "find_symbol", "find_callers", "trace_dependencies")
NEIGHBOURHOOD = ("inspect_symbol", *DIRECTED)
DECISION = (
    "You are searching an unfamiliar repository. A first search has already run and its results are "
    "below. Choose the single next retrieval action most likely to surface the answer.\n"
    "Reply with one JSON object and nothing else:\n"
    '{{"action": "{actions}", "argument": "...", "why": "..."}}\n'
    "search_concept takes a query phrased in code vocabulary. The others each take one unqualified "
    "symbol name.{extra} Prefer a structural action when the question asks about callers, "
    "dependencies, chains, or impact and a plausible seed symbol appears in the results. Do not "
    "answer the question."
)
NEIGHBOURHOOD_HINT = (
    " inspect_symbol returns both sides of a symbol at one hop, so use it when you have a candidate "
    "symbol but do not know whether the answer lies among its callers or its callees."
)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def words(text):
    found = set()
    for token in WORD.findall(text or ""):
        found.add(token.lower())
        found.update(part.lower() for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", token)
                     if len(part) > 2)
    return found


def call(client, name, arguments, errors=None):
    """Tool errors are recorded, never silently scored as an empty result."""
    result = client.request("tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        if errors is not None:
            errors.append({"tool": name, "arguments": arguments,
                           "error": str(result.get("structuredContent"))[:200]})
        return []
    content = result.get("structuredContent") or {}
    return content.get("results", [])


def identities(name, rows):
    """Every symbol identity a tool result names, whatever the tool's row shape."""
    found = []
    for row in rows:
        if name == "search_concept":
            found.append(study_a.row_identity(row))
        elif name == "find_symbol":
            found.append((row.get("path"), row.get("name")))
        elif name == "find_callers":
            found.append((row.get("path"), row.get("caller")))
        elif name == "trace_dependencies":
            found.append((row.get("path"), row.get("caller")))
            found.append((row.get("path"), row.get("callee")))
        elif name == "inspect_symbol":
            found.append((row.get("path"), (row.get("symbol") or "::").split("::")[-1]))
    return [pair for pair in found if pair[0] and pair[1]]


def rank_of(found, relevant):
    for position, pair in enumerate(found, 1):
        if pair in relevant:
            return position
    return None


def evidence_digest(rows, limit):
    lines = []
    for row in rows[:limit]:
        symbol = row.get("symbol") or {}
        lines.append(f"- {symbol.get('symbol', row.get('path'))} "
                     f"lines {row.get('start_line')}-{row.get('end_line')} "
                     f"callers={symbol.get('callers', '?')} callees={symbol.get('callees', '?')}")
    return "\n".join(lines) or "- (nothing returned)"


def run(args):
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    graded = [task for task in questions if study_a.relevant_symbols(task)]
    entries = json.loads(args.reformulations.read_text(encoding="utf-8"))["reformulations"]
    cache = args.cache.resolve()
    cells, spend, errors, malformed = {}, 0.0, [], []
    tokens = {"input": 0, "output": 0}
    before = comparison_runner.source_fingerprint(args.root)
    command = [str(args.server), "--root", str(args.root), "--profile", "D",
               "--ranker", args.ranker, "--timeout-seconds", str(args.timeout)]
    if args.ranker != "lexical":
        command += ["--semantic-command", json.dumps(args.semantic_command)]
    with tempfile.TemporaryFile(mode="w+") as stderr:
        client = benchmark.MCP(command, dict(os.environ, RETRIEVAL_SEMANTIC_CACHE_DIR=str(cache)),
                               args.root, stderr, args.timeout + 60)
        try:
            client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "study-a2", "version": "1"}})
            for repetition in range(1, args.repetitions + 1):
                for task in graded:
                    relevant = study_a.relevant_symbols(task)
                    rewrite = " ".join([*entries[task["id"]]["concepts"],
                                        *entries[task["id"]]["candidate_identifiers"],
                                        *entries[task["id"]]["terms"]])
                    stage0 = call(client, "search_concept",
                                  {"query": task["question"], "limit": args.limit})
                    stage1 = call(client, "search_concept", {"query": rewrite, "limit": args.limit})
                    actions = NEIGHBOURHOOD if args.surface == "neighbourhood" else DIRECTED
                    instruction = DECISION.format(
                        actions="|".join(actions),
                        extra=NEIGHBOURHOOD_HINT if args.surface == "neighbourhood" else "")
                    prompt = (f"{instruction}\n\nQuestion: {task['question']}\n\n"
                              f"Search already tried: {rewrite}\n\nResults:\n"
                              f"{evidence_digest(stage1, args.limit)}")
                    text, usage = make_reformulations.ask(args.model, args.variant, prompt,
                                                          args.decision_timeout)
                    spend += (usage.get("cost_usd") or 0)
                    tokens["input"] += usage.get("input", 0) or 0
                    tokens["output"] += usage.get("output", 0) or 0
                    try:
                        decision = make_reformulations.parse(text, ("action", "argument"))
                    except ValueError:
                        # One bad reply must not discard the spend on every other question.
                        malformed.append({"task": task["id"], "repetition": repetition,
                                          "text": text[:200]})
                        continue
                    action = (decision["action"] if decision["action"] in actions
                              else "search_concept")
                    argument = str(decision["argument"])
                    payload = ({"query": argument, "limit": args.limit}
                               if action == "search_concept" else
                               {"name": argument} if action == "inspect_symbol"
                               else {"name": argument, "limit": args.limit})
                    stage2 = call(client, action, payload, errors)
                    # Vocabulary the model could only have taken from what stage 1 returned.
                    seen = set()
                    for row in stage1[: args.limit]:
                        symbol = row.get("symbol") or {}
                        seen |= words(symbol.get("symbol") or row.get("path", ""))
                    introduced = words(argument)
                    cells[f"{task['id']}#{repetition}"] = {
                        "task_id": task["id"],
                        "repetition": repetition,
                        "category": task["category"],
                        "stage0_rank": rank_of(identities("search_concept", stage0), relevant),
                        "stage1_rank": rank_of(identities("search_concept", stage1), relevant),
                        "stage2_rank": rank_of(identities(action, stage2), relevant),
                        "action": action,
                        "argument": argument,
                        "why": str(decision.get("why", ""))[:300],
                        "vocabulary": {
                            "from_question": sorted(introduced & words(task["question"])),
                            "from_rewrite": sorted(introduced & words(rewrite)
                                                   - words(task["question"])),
                            "from_evidence": sorted(introduced & seen - words(task["question"])
                                                    - words(rewrite)),
                            "novel": sorted(introduced - words(task["question"]) - words(rewrite)
                                            - seen),
                        },
                    }
        finally:
            client.close()
    if comparison_runner.source_fingerprint(args.root) != before:
        raise RuntimeError("the corpus changed while it was being ranked; results are void")
    solved = lambda key: sum(1 for cell in cells.values()
                             if cell[key] and cell[key] <= args.limit)
    cumulative = sum(1 for cell in cells.values()
                     if any(cell[key] and cell[key] <= args.limit
                            for key in ("stage1_rank", "stage2_rank")))
    return {
        "version": "study-a2-v1",
        "questions_graded": len(cells),
        "limit": args.limit,
        "ranker": args.ranker,
        "corpus": before,
        "decision_model": args.model,
        "decision_cost_usd": round(spend, 6),
        "tokens": tokens,
        "tool_errors": errors,
        "malformed_decisions": malformed,
        "solved": {"stage0": solved("stage0_rank"), "stage1": solved("stage1_rank"),
                   "stage2_alone": solved("stage2_rank"), "stage1_or_stage2": cumulative},
        "actions": {action: sum(1 for cell in cells.values() if cell["action"] == action)
                    for action in NEIGHBOURHOOD},
        "by_category": {
            category: {
                "n": sum(1 for cell in cells.values() if cell["category"] == category),
                "stage1": sum(1 for cell in cells.values() if cell["category"] == category
                              and cell["stage1_rank"]),
                "stage1_or_stage2": sum(1 for cell in cells.values() if cell["category"] == category
                                        and (cell["stage1_rank"] or cell["stage2_rank"])),
            }
            for category in sorted({cell["category"] for cell in cells.values()})},
        "vocabulary_totals": {
            source: round(statistics.fmean(len(cell["vocabulary"][source])
                                           for cell in cells.values()), 2)
            for source in ("from_question", "from_rewrite", "from_evidence", "novel")},
        "cells": cells,
        "limitations": "One evidence-conditioned step, one seed search, one model. A deeper loop "
                       "could recover more; this measures the marginal value of the first look at "
                       "retrieved rows, not the ceiling of iteration.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--reformulations", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--server", type=Path, default=base.parent / "target/release/retrieval-mcp")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--semantic-command", type=benchmark.command_array, required=True)
    parser.add_argument("--ranker", default="lexical", choices=study_a.RANKERS)
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--variant", default="high")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--decision-timeout", type=int, default=180)
    parser.add_argument("--surface", default="directed",
                        choices=("directed", "neighbourhood"))
    parser.add_argument("--allow-model-usage", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.allow_model_usage:
        raise ValueError("--allow-model-usage is required; nothing was sent")
    args.root = args.root.resolve(strict=True)
    # Refuse before the model spend, not after it.
    if args.output and args.output.exists():
        raise FileExistsError(args.output)
    result = run(args)
    if args.output:
        benchmark.write_json(args.output, result)
    print(json.dumps({key: result[key] for key in
                      ("solved", "actions", "by_category", "vocabulary_totals",
                       "decision_cost_usd")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
