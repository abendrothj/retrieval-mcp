#!/usr/bin/env python3
"""Where the gold lands under each ranker, in hop tiers, per query condition. No model calls.

`page_position.py` reads archived payloads to ask what a page cut would have cost. This asks the
prior question against a live index: for one question, under each ranker, does the page carry the
gold *definition*, only its *file*, or neither - and how does that change when the query is the
user's own words rather than a reformulation into code vocabulary.

**The tiers are hop tiers, not rank buckets.** A rank maps to round trips in three steps rather
than linearly, which is why they are counted this way:

  identity_on_page  the gold definition is on the page: no further round trip is required, and
                    *where* on the page is nearly free - `runs/degrade-control-20260924` scrambled
                    the row order and scored 29/29 against 29/29, with the gold pushed to rank 10
                    in six trials and no answer changing.
  file_only         the gold's file is on the page and its definition is not: one recovery hop,
                    since `read_source` or `find_callers` on a named row reaches it.
  neither           a re-query, possibly several.

A dependent hop is ~15,000 input tokens against ~2,400 for a 20 KB payload
(`runs/token-metric-20260921`), which is why the tier split and not top-1 rank is the readout that
maps to cost.

**Matching is exact and structural, never substring over concatenated text.** A row's identity is
compared to the gold's full `path::Name`, and a row's path to the gold's path, both by equality,
both read from `structuredContent.results`. `end_to_end.unretrieved` is deliberately not used
here: it is conjunctive over a request-plus-body concatenation, so a path the agent typed itself
counts as retrieval, and a gold whose bare name appears anywhere in its own file collapses
`identity_on_page` into `file_only` - which is exactly where this contrast lives.

    ranker_tiers.py --questions SUITE --server BIN --semantic-command '["/usr/bin/true"]' \\
        --rankers lexical semantic --conditions title --output tiers.json
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

TIERS = ("identity_on_page", "file_only", "neither")


def page(server, corpus, ranker, semantic_command, query, limit, timeout):
    """One `search_concept` call against a freshly launched server. Returns (rows, error)."""
    command = [str(server), "--root", str(corpus), "--ranker", ranker,
               "--semantic-command", json.dumps(semantic_command),
               "--timeout-seconds", str(timeout)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    handshake = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "ranker_tiers", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search_concept", "arguments": {"query": query, "limit": limit}}},
    ]
    out, _ = process.communicate("\n".join(json.dumps(m) for m in handshake) + "\n",
                                 timeout=timeout + 300)
    rows, error = [], None
    for line in out.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") != 2:
            continue
        result = payload.get("result") or {}
        text = ((result.get("content") or [{}])[0].get("text") or "")
        if result.get("isError") or text.startswith("error:"):
            error = text[:200]
        for row in (result.get("structuredContent") or {}).get("results") or []:
            symbol = row.get("symbol")
            identity = symbol.get("symbol") if isinstance(symbol, dict) else symbol
            rows.append({"path": row.get("path"), "identity": identity})
    return rows, error


def positions(rows, gold):
    """(identity rank, file rank) by exact comparison; 1-based, None when absent."""
    gold_path = gold.split("::")[0]
    identity = file_rank = None
    for index, row in enumerate(rows, start=1):
        if identity is None and row.get("identity") == gold:
            identity = index
        if file_rank is None and row.get("path") == gold_path:
            file_rank = index
    return identity, file_rank


def tier(identity, file_rank):
    if identity is not None:
        return "identity_on_page"
    if file_rank is not None:
        return "file_only"
    return "neither"


def conditions_for(task, archived):
    """Query conditions. `title` is the question's first line; `agent` are archived model queries.

    A LOC-BENCH question is a whole GitHub issue - 16,251 characters for one of them, past the
    server's 16 KiB argument limit - so the raw question is not a query any client can send. The
    title is the user's own words; the archived queries are the model's reformulation into code
    vocabulary, which is the contrast the ranker bake-off's conclusion rests on.
    """
    found = {"title": [task["question"].split("\n")[0].strip()]}
    queries = (archived or {}).get(task["id"])
    if queries:
        found["agent"] = list(queries)
    return found


def report(args):
    tasks = json.loads(args.questions.read_text(encoding="utf-8"))
    archived = json.loads(args.archived_queries.read_text(encoding="utf-8")) \
        if args.archived_queries else {}
    rows, tally = [], {}
    for task in tasks:
        gold = task["expected_json"]["answer"]
        corpus = args.root or task.get("corpus")
        if not corpus:
            raise ValueError(f"{task['id']} pins no corpus and --root was not given")
        for condition, queries in conditions_for(task, archived).items():
            if args.conditions and condition not in args.conditions:
                continue
            for ranker in args.rankers:
                best_identity = best_file = None
                errors = []
                for query in queries:
                    page_rows, error = page(args.server, corpus, ranker, args.semantic_command,
                                            query, args.limit, args.timeout)
                    if error:
                        errors.append(error)
                    identity, file_rank = positions(page_rows, gold)
                    if identity and (best_identity is None or identity < best_identity):
                        best_identity = identity
                    if file_rank and (best_file is None or file_rank < best_file):
                        best_file = file_rank
                label = tier(best_identity, best_file)
                rows.append({"question": task["id"], "condition": condition, "ranker": ranker,
                             "identity_rank": best_identity, "file_rank": best_file,
                             "tier": label, "queries": len(queries),
                             "errors": errors})
                counts = tally.setdefault(f"{condition}/{ranker}", dict.fromkeys(TIERS, 0))
                counts[label] += 1
    return {"version": "ranker-tiers-v1", "questions": len(tasks), "limit": args.limit,
            "rankers": list(args.rankers), "tally": tally, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--semantic-command", required=True,
                        type=lambda value: json.loads(value))
    parser.add_argument("--root", type=Path, default=None,
                        help="one corpus for every question; omit when questions pin their own")
    parser.add_argument("--archived-queries", type=Path, default=None,
                        help="{question id: [query]} from an archived run, for the agent condition")
    parser.add_argument("--rankers", nargs="+", default=["lexical"])
    parser.add_argument("--conditions", nargs="*", default=None, choices=["title", "agent"])
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text if not args.output else json.dumps(result["tally"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
