#!/usr/bin/env python3
"""Rank one authored question set with several retrieval systems and score them identically.

`study_a.py` varies one ranker inside this server. This is its multi-arm sibling: the variable is
the whole retrieval system. Arms are declared on the command line, every arm answers the raw
question text over one byte-identical corpus, and one function scores all of them. No model is
called, no network is touched, and nothing writes into the corpus under test.

**Attribution is the place symmetry is usually lost, so it is shared.** This server returns a
`symbol` block naming the definition it matched; `zvec-grep` returns a line range and nothing else.
Scoring one arm on its own symbol block and the other on bare paths would compare bookkeeping
rather than retrieval, so the `symbol` block is discarded. Every row from every arm is reduced to
`(path, start_line)` and resolved by `attribution()` below, which **reuses** the project's existing
enclosing-definition logic rather than adding a fourth copy of it: `audit_failures.enclosing()` for
the scan that finds the definition containing a line, and `audit_failures.declared_name()` /
`quality_pass.DEFINITION` for the one case `enclosing()` deliberately does not cover - a row that
*begins on* the declaration line rather than inside the body, which is the common case here because
a retrieved chunk usually is a definition. Three ledger defects came from private copies of this
logic; this is not a fourth.

**zvec-grep keeps its workspace index under the root it indexes** (`<root>/.zvec-grep`, per
`zg help environment`), and a retrieval system must never be able to write into the corpus that
judges it. The zvec arm therefore copies the corpus under `--state`, asserts the copy carries the
corpus's own source fingerprint, indexes the copy, and attributes every returned path back to the
original corpus. `ZVEC_GREP_HOME` and `ZVEC_GREP_MODEL_CACHE` are likewise forced outside the
corpus.

Gold is symbol identity, path-qualified, so returning the right 2000-line file is not a hit. A
question whose gold names no file of this corpus is not scored and is reported as skipped with its
reason, because a benchmark that silently drops what it cannot grade is worse than one that grades
nothing.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
import time

import audit_failures
import benchmark
import comparison_runner
import quality_pass

CUTOFFS = (1, 3, 5, 10)
# Every language the server indexes. A narrower list silently drops a whole suite's gold: the Go
# etcd set graded zero questions under the four-suffix version this shared with `study_a`.
SOURCE_SUFFIXES = (".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
                   ".go", ".java", ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")
KINDS = ("mcp", "zvec")
# `zg query` in 0.2.2 has no JSON output mode - the CLI emits "agent markdown" and `--human` only
# makes it wordier. Its hit header is the machine-readable part: `#1 matchedBy=fts+vector a/b.rs:12-40`.
ZVEC_HIT = re.compile(r"^#(\d+)\s+(?:matchedBy=\S+\s+)?(.+?):(\d+)-(\d+)$")
ZVEC_EMBEDDING = "local/potion-code-16m-v2"
# A chunk may begin on the doc comment, attribute or decorator that introduces a definition - v0.1.2
# put doc comments inside concept chunks on purpose - so these prefixes never terminate the forward
# scan for the code a row is pointing at.
INTRODUCER = ("//", "/*", "*", "#", "@")
# `audit_failures.declared_name` recognises only `fn` for Rust, and deliberately so: a call site is
# never inside a struct or trait body, so admitting type items there would invent owners for
# caller attribution. A retrieved *chunk*, though, very often is a type item, and `zvec-grep`'s
# chunker in particular prefers whole `impl` blocks. Refusing to name those would score one arm's
# chunk granularity rather than its ranking - the asymmetry this module exists to avoid - so the
# chunk-start case adds them back. This is declaration recognition, not enclosing logic, and
# nothing below reimplements the latter.
RUST_ITEMS = (
    re.compile(r"\s*(?:pub(?:\s*\([^)]*\))?\s+)?(?:unsafe\s+)?"
               r"(?:struct|enum|trait|union|type|mod)\s+([A-Za-z_]\w*)"),
    # `impl<'a> Parser<'a> {` and `impl Display for Range {` both name the type they belong to.
    re.compile(r"\s*impl(?:\s*<[^>]*>)?\s+(?:.+?\s+for\s+)?([A-Za-z_]\w*)"),
)


def parse_arm(text):
    """`mcp:<name>=<binary>` or `zvec:<name>=<binary>`."""
    kind, _, rest = text.partition(":")
    name, _, binary = rest.partition("=")
    if kind not in KINDS or not name or not binary:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not <{'|'.join(KINDS)}>:<name>=<path-to-binary>")
    return {"kind": kind, "name": name, "binary": Path(binary)}


def gold_identities(task):
    """Every (path, leaf) the gold asserts. Set- and object-valued golds flatten in declaration order."""
    found = []
    for identity in quality_pass.flatten((task.get("expected_json") or {}).get("answer")):
        path, separator, rest = identity.partition("::")
        if separator and path.endswith(SOURCE_SUFFIXES):
            pair = (path, rest.split("::")[-1])
            if pair not in found:
                found.append(pair)
    return found


def leading_declaration(path, text):
    """The definition this line *opens*, or None.

    `enclosing()` answers "which definition contains this line" and deliberately steps over the
    declaration itself, because a call site is never its own declaration. A retrieved chunk very
    often *is* the declaration, and reading backwards from it credits the enclosing class instead
    of the method - the exact one-scope-too-high defect already in the ledger, arriving from the
    other direction. The tables are the project's own: `audit_failures.declared_name` for the
    brace languages, `quality_pass.DEFINITION` for Python, and `RUST_ITEMS` for the shapes
    neither needs to know about.
    """
    if path.suffix in audit_failures.BRACE_SUFFIXES:
        name = audit_failures.declared_name(text, path.suffix)
        if name is None and path.suffix == ".rs":
            name = next((match.group(1) for match in
                         (expression.match(text) for expression in RUST_ITEMS) if match), None)
        # `enclosing()` refuses a binding that holds no callable, because a call written into a
        # local const belongs to the function around it. A retrieved *chunk* is a different
        # question: `export const DEFAULT_TAB_SIZE = 4;` at module level is what an arm returned
        # and what a gold may name, so a binding that starts its own line is credited here.
        if name is None and path.suffix in audit_failures.SCRIPT_SUFFIXES and text[:1].strip():
            match = audit_failures.SCRIPT_BINDING.match(text)
            name = match.group(1) if match else None
        return name
    match = quality_pass.DEFINITION.match(text)
    return next((group for group in match.groups() if group), None) if match else None


def attribution(path, line):
    """The definition a returned row points at, for every arm alike. Leaf name, or None."""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    if not 1 <= line <= len(source):
        return None
    position = line - 1
    while position < len(source):
        stripped = source[position].strip()
        if stripped and not stripped.startswith(INTRODUCER):
            break
        position += 1
    if position >= len(source):
        position = line - 1
    text = source[position]
    declared = leading_declaration(path, text)
    owner = audit_failures.enclosing(path, position + 1)
    # A declaration credits itself only if it really opened a scope. TypeScript spells a
    # module-level definition (`export const DEFAULT_TAB_SIZE = 4;`) exactly as it spells a local
    # binding (`const value = buffer.getLineContent(n);`), and crediting the second would invent a
    # definition named `value` for a row that belongs to the method around it. Nothing enclosing
    # the line makes it a definition by elimination; otherwise a statement ending in a semicolon
    # opened no body, which is confirmed by asking who owns the line below it.
    if declared and (owner is None or not text.strip().endswith(";")
                     or audit_failures.enclosing(path, position + 2) == declared):
        return declared
    return owner


def row_identity(root, row):
    """(path, definition) for one returned row; definition is None when nothing encloses it."""
    relative = str(row.get("path") or "").lstrip("./")
    resolved = (root / relative).resolve()
    if not relative or not resolved.is_relative_to(root) or not resolved.is_file():
        return (relative, None)
    start = row.get("start_line")
    return (relative, attribution(resolved, start) if isinstance(start, int) else None)


def spell(identity):
    return "::".join(part for part in identity if part) if identity[1] else f"{identity[0]}::?"


def mcp_arm(arm, root, questions, limit, timeout, state):
    """Drive retrieval-mcp over MCP stdio exactly as study_a.py does: profile D, lexical ranker."""
    command = [str(arm["binary"]), "--root", str(root), "--profile", "D", "--ranker", "lexical",
               "--timeout-seconds", str(timeout)]
    rows, latencies, payload = {}, {}, {}
    cold = None
    cache = state / f"{arm['name']}-semantic-cache"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(mode="w+") as stderr:
        environment = dict(os.environ, RETRIEVAL_SEMANTIC_CACHE_DIR=str(cache))
        client = benchmark.MCP(command, environment, root, stderr, timeout + 60)
        try:
            client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "study-b", "version": "1"}})
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            for task in questions:
                started = time.monotonic()
                result = client.request("tools/call", {"name": "search_concept", "arguments": {
                    "query": task["question"], "limit": limit}})
                elapsed = (time.monotonic() - started) * 1000
                if result.get("isError"):
                    raise RuntimeError(f"{arm['name']} failed on {task['id']}: {result}")
                returned = result["structuredContent"]["results"]
                if cold is None:
                    cold = elapsed  # The first call pays for building the index.
                else:
                    latencies[task["id"]] = elapsed
                rows[task["id"]] = [{"path": hit.get("path"), "start_line": hit.get("start_line"),
                                     "end_line": hit.get("end_line")} for hit in returned]
                payload[task["id"]] = len(json.dumps(returned, ensure_ascii=False).encode())
        finally:
            client.close()
    return {"kind": "mcp", "rows": rows, "bytes": payload, "latencies": latencies, "cold_ms": cold,
            "index_build_ms": None,
            "index_build": "folded into the cold first query; this server indexes on demand"}


def zvec_environment(state, model_cache):
    home = state / "zvec-home"
    home.mkdir(parents=True, exist_ok=True)
    # The defect this repeats: zvec writing state into the corpus it is ranking. Home and model
    # cache are forced outside it, and the corpus copy it indexes is under --state.
    return dict(os.environ, ZVEC_GREP_HOME=str(home), ZVEC_GREP_MODEL_CACHE=str(model_cache),
                ZVEC_GREP_MODE="direct", NO_COLOR="1")


def zvec_workspace(arm, root, state, model_cache, embedding, timeout):
    """Copy the corpus outside itself, prove the copy is the corpus, and index the copy."""
    workspace = state / f"{arm['name']}-corpus"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(root, workspace, symlinks=False)
    if comparison_runner.source_fingerprint(workspace) != comparison_runner.source_fingerprint(root):
        raise RuntimeError("the zvec working copy does not carry the corpus fingerprint")
    environment = zvec_environment(state, model_cache)
    command = [str(arm["binary"]), "index", str(workspace), "--embedding", embedding,
               "--mode", "direct", "--model-cache", str(model_cache)]
    started = time.monotonic()
    built = subprocess.run(command, cwd=workspace, env=environment, capture_output=True,
                           text=True, timeout=timeout)
    elapsed = (time.monotonic() - started) * 1000
    if built.returncode != 0:
        raise RuntimeError(f"{arm['name']} index failed: {built.stdout}\n{built.stderr}")
    return workspace, elapsed, command


def zvec_rows(text):
    """(path, start_line, end_line) per hit, from the agent-markdown headers `zg query` prints."""
    found = []
    for line in text.splitlines():
        match = ZVEC_HIT.match(line.strip())
        if match:
            found.append({"path": match.group(2), "start_line": int(match.group(3)),
                          "end_line": int(match.group(4))})
    return found


def zvec_arm(arm, root, questions, limit, timeout, state, model_cache, embedding, preview):
    workspace, index_ms, index_command = zvec_workspace(arm, root, state, model_cache, embedding,
                                                        timeout)
    environment = zvec_environment(state, model_cache)
    rows, latencies, payload = {}, {}, {}
    cold = None
    for task in questions:
        command = [str(arm["binary"]), "query", task["question"], "--limit", str(limit),
                   "--mode", "direct", "--refresh", "off", "--preview", preview,
                   "--model-cache", str(model_cache)]
        started = time.monotonic()
        answered = subprocess.run(command, cwd=workspace, env=environment, capture_output=True,
                                  text=True, timeout=timeout)
        elapsed = (time.monotonic() - started) * 1000
        if answered.returncode != 0:
            raise RuntimeError(f"{arm['name']} failed on {task['id']}: {answered.stderr}")
        if cold is None:
            cold = elapsed  # The first query pays for loading the embedding model.
        else:
            latencies[task["id"]] = elapsed
        rows[task["id"]] = zvec_rows(answered.stdout)
        payload[task["id"]] = len(answered.stdout.encode())
    return {"kind": "zvec", "rows": rows, "bytes": payload, "latencies": latencies, "cold_ms": cold,
            "index_build_ms": round(index_ms, 1),
            "index_build": " ".join(index_command)}


def measures(ranks):
    """One scoring row. Bucket rows and the overall row are the same shape, so buckets sum."""
    return {"n": len(ranks),
            "mrr": round(statistics.fmean(1 / rank if rank else 0.0 for rank in ranks), 4)
                   if ranks else None,
            **{f"recall@{cutoff}": sum(1 for rank in ranks if rank and rank <= cutoff)
               for cutoff in CUTOFFS}}


def summarize(cells):
    result = measures([cell["rank"] for cell in cells.values()])
    categories = {}
    for cell in cells.values():
        categories.setdefault(cell["category"], []).append(cell["rank"])
    result["by_category"] = {name: measures(ranks) for name, ranks in sorted(categories.items())}
    return result


def economics(outcome):
    latencies = list(outcome["latencies"].values())
    payload = list(outcome["bytes"].values())
    return {
        "index_build_ms": outcome["index_build_ms"],
        "index_build": outcome["index_build"],
        "cold_first_query_ms": round(outcome["cold_ms"], 1) if outcome["cold_ms"] else None,
        "warm_query_ms_median": round(statistics.median(latencies), 1) if latencies else None,
        "warm_query_ms_max": round(max(latencies), 1) if latencies else None,
        "bytes_per_query_mean": round(statistics.fmean(payload)) if payload else None,
        "queries": len(outcome["rows"]),
    }


def evaluate(root, graded, outcome):
    """Score one arm's rows. The only scoring path; a fake arm in the tests uses it unchanged."""
    cells = {}
    for task in graded:
        gold = gold_identities(task)
        ranked = outcome["rows"].get(task["id"], [])
        identities = [row_identity(root, row) for row in ranked]
        positions = {}
        for identity in gold:
            positions["::".join(identity)] = next(
                (place for place, got in enumerate(identities, 1) if got == identity), None)
        hit = [place for place in positions.values() if place]
        cells[task["id"]] = {
            "category": task["category"],
            "gold": ["::".join(identity) for identity in gold],
            # Rank of the first gold definition. An exhaustive gold is not a ranking task, so
            # `gold_ranks` carries each identity separately rather than pretending otherwise.
            "rank": min(hit) if hit else None,
            "gold_ranks": positions,
            "returned": len(ranked),
            "bytes": outcome["bytes"].get(task["id"]),
            "ranked": [spell(identity) for identity in identities],
        }
    return {"effectiveness": summarize(cells), "economics": economics(outcome), "cells": cells}


def defines(root, path, name):
    """Whether any line of this corpus file opens a definition with this leaf name.

    Deliberately the same table `attribution()` uses, not `quality_pass.definitions()`: gradability
    must mean "some arm could produce this identity". The ripgrep index knows only
    `^\\s*(pub\\s+)?(async\\s+)?fn`, so a gold naming a `pub(crate) fn` would be declared ungradable
    while every arm was in fact able to hit it - a skip that looks like an authoring error and is
    actually a harness one.
    """
    source = (root / path).read_text(encoding="utf-8", errors="ignore").splitlines()
    return any(leading_declaration(root / path, text) == name for text in source)


def gradable(task, root):
    """Why this question cannot be scored against this corpus, or None when it can."""
    gold = gold_identities(task)
    if not gold:
        return "gold names no path-qualified symbol (null or prose gold)"
    absent = sorted({path for path, _ in gold if not (root / path).is_file()})
    if absent:
        return f"gold path not in this corpus: {', '.join(absent)}"
    unverified = sorted({f"{path}::{name}" for path, name in gold if not defines(root, path, name)})
    if unverified:
        return f"corpus file defines no such symbol: {', '.join(unverified)}"
    return None


def report(args):
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    state = args.state.resolve()
    # A retrieval system must never be able to alter the corpus it is judged against: the first run
    # of study_a wrote 93 MB of vectors into the corpus before this check existed.
    if state == args.root or state.is_relative_to(args.root):
        raise ValueError("the arm state directory must live outside the corpus")
    model_cache = args.zvec_model_cache.resolve()
    if model_cache == args.root or model_cache.is_relative_to(args.root):
        raise ValueError("the zvec model cache must live outside the corpus")
    state.mkdir(parents=True, exist_ok=True)
    skipped = [{"id": task["id"], "reason": reason} for task in questions
               if (reason := gradable(task, args.root))]
    refused = {entry["id"] for entry in skipped}
    graded = [task for task in questions if task["id"] not in refused]
    before = comparison_runner.source_fingerprint(args.root)
    conditions = {}
    for arm in args.arms:
        if arm["name"] in conditions:
            raise ValueError(f"two arms are named {arm['name']}")
        if arm["kind"] == "mcp":
            outcome = mcp_arm(arm, args.root, questions, args.limit, args.timeout, state)
        else:
            outcome = zvec_arm(arm, args.root, questions, args.limit, args.timeout, state,
                               model_cache, args.zvec_embedding, args.zvec_preview)
        conditions[arm["name"]] = {"kind": arm["kind"], "binary": str(arm["binary"]),
                                   **evaluate(args.root, graded, outcome)}
    if comparison_runner.source_fingerprint(args.root) != before:
        raise RuntimeError("the corpus changed while it was being ranked; results are void")
    return {
        "version": "study-b-v1",
        "corpus": before,
        "corpus_root": str(args.root),
        "questions": len(questions),
        "questions_graded": [task["id"] for task in graded],
        "questions_skipped": skipped,
        "limit": args.limit,
        "conditions": conditions,
        # Per question, per arm, so a regression is traceable to the question that caused it.
        "ranks": {task["id"]: {name: condition["cells"][task["id"]]["rank"]
                               for name, condition in conditions.items()}
                  for task in graded},
        "limitations":
            "One corpus, one authored question set, symbol-level gold, queries are the raw question "
            "text - which understates every arm as an agent uses it, since a model rewrites a "
            "description into candidate identifiers before searching. Rank is the rank of the first "
            "gold definition, so an exhaustive-caller question is scored as a find-one task; "
            "`gold_ranks` carries the rest. Byte counts are each arm's own default payload "
            "(this server's JSON rows carry excerpts, `zg query --preview none` carries headers "
            "only), so they compare what an agent would receive, not equal content. Every arm is "
            "queried on every question; only questions whose gold names this corpus are scored. "
            "Deterministic, so no repetitions are run.",
    }


def render(result):
    lines = [f"corpus {result['corpus_root']} sha256 {result['corpus']['sha256'][:12]} "
             f"({result['corpus']['files']} files)",
             f"graded {len(result['questions_graded'])}/{result['questions']} questions; "
             f"{len(result['questions_skipped'])} not gradable against this corpus"]
    for entry in result["questions_skipped"]:
        lines.append(f"  skip {entry['id']}: {entry['reason']}")
    header = "  ".join(f"{f'r@{cutoff}':>5}" for cutoff in CUTOFFS)
    for name, condition in result["conditions"].items():
        effectiveness, money = condition["effectiveness"], condition["economics"]
        lines.append("")
        lines.append(f"[{name}] {condition['kind']}")
        lines.append(f"  {'bucket':<26} {'n':>3} {'mrr':>7}  {header}")
        counts = "  ".join(f"{effectiveness[f'recall@{cutoff}']:>5}" for cutoff in CUTOFFS)
        lines.append(f"  {'OVERALL':<26} {effectiveness['n']:>3} "
                     f"{effectiveness['mrr'] if effectiveness['mrr'] is not None else '-':>7}  {counts}")
        for bucket, row in effectiveness["by_category"].items():
            counts = "  ".join(f"{row[f'recall@{cutoff}']:>5}" for cutoff in CUTOFFS)
            lines.append(f"  {bucket:<26} {row['n']:>3} "
                         f"{row['mrr'] if row['mrr'] is not None else '-':>7}  {counts}")
        built = f"{money['index_build_ms']} ms" if money["index_build_ms"] is not None else "not a separate step"
        lines.append(f"  index build {built} ({money['index_build']})")
        lines.append(f"  cold first query {money['cold_first_query_ms']} ms; "
                     f"warm median {money['warm_query_ms_median']} ms; "
                     f"warm max {money['warm_query_ms_max']} ms; "
                     f"bytes/query {money['bytes_per_query_mean']} over {money['queries']} queries")
    if result["ranks"]:
        lines.append("")
        names = list(result["conditions"])
        lines.append(f"  {'question':<40} " + "  ".join(f"{name:>12}" for name in names))
        for task_id, row in result["ranks"].items():
            lines.append(f"  {task_id:<40} " + "  ".join(
                f"{row[name] if row[name] else '-':>12}" for name in names))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--arms", nargs="+", type=parse_arm, required=True,
                        help="mcp:<name>=<binary> or zvec:<name>=<binary>")
    parser.add_argument("--state", type=Path, required=True,
                        help="per-arm working state; must be outside the corpus")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--zvec-model-cache", type=Path,
                        default=base.parent / "runs/heldout-workspace-20260912/shared/zvec-model-cache")
    parser.add_argument("--zvec-embedding", default=ZVEC_EMBEDDING)
    parser.add_argument("--zvec-preview", default="none", choices=("none", "short", "full"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.root = args.root.resolve(strict=True)
    for arm in args.arms:
        # Each arm runs with the corpus as its working directory, so a binary given relative to
        # the caller's directory must be resolved before it is launched, not after.
        arm["binary"] = arm["binary"].resolve(strict=True)
    result = report(args)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, result)
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
