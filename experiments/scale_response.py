#!/usr/bin/env python3
"""Vary corpus size and nothing else, offline, for both retrieval surfaces.

Three agent-level studies compared this server against a native shell arm on three corpora, and
each of them varied size together with language, question author, client and suite vintage. So
`runs/linux-agent-20260919` could say the claim reverses at 86,602 files and could not say that
size is why. This instrument answers the narrower, identified question: hold the *retrieval
behaviour* fixed and move only the corpus.

Two behaviours are replayed against every rung of a nested ladder whose smallest rung already
contains every gold and evidence file:

  * **replay** - the exact calls an archived agent run issued, shell commands for the native arm
    and tool calls for this server, re-executed verbatim. Fixed behaviour, varying corpus: this
    isolates what size does to the cost and the evidence of one retrieval action.
  * **fresh** - `search_concept` asked the raw question at each rung, scored by the rank of the
    first gold definition. This is the server-owned quantity - does the ranker still put the answer
    in front of the agent once the distractors multiply - and it needs no model.

Nothing here calls a model or the network, and no arm can write into a corpus: rungs are hard-link
trees and every tool runs read-only against them.
"""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import tempfile
import time

import benchmark
import study_b

# What a shell arm's output costs the model is not what it printed: Codex forwards a truncated
# head. The kernel run's own fit put the share that reaches context at ~9%, and the recorded cap
# is 1 MiB, so both the raw and the capped figure are reported rather than one of them chosen.
SHELL_CAP = 1 << 20


def gold_pairs(task):
    """Every (path, leaf) the gold asserts, as study_b spells them."""
    return study_b.gold_identities(task)


def visible(text, pairs):
    """Gold identities a payload exposes, judged as end_to_end judges a hit."""
    return [pair for pair in pairs if pair[0] in text and pair[1] in text]


def recorded_calls(run):
    """Per question and arm, the calls an archived Codex run actually made."""
    found = defaultdict(lambda: defaultdict(list))
    for state_path in sorted(Path(run).glob("trial-*/run.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        events = state_path.parent / "codex-events.jsonl"
        if state.get("status") != "completed" or not events.is_file():
            continue
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            item = (json.loads(line).get("item") or {})
            if item.get("type") == "command_execution":
                found[state["task_id"]]["shell"].append({"command": item.get("command") or ""})
            elif item.get("type") == "mcp_tool_call":
                found[state["task_id"]]["mcp"].append({
                    "tool": item.get("tool") or item.get("name"),
                    "arguments": item.get("arguments") or item.get("input") or {}})
    return found


def replay_shell(command, root, timeout, home=None):
    """Re-run one recorded shell command against one rung, sealed three ways.

    `stdin` is /dev/null because ripgrep given no path argument reads standard input instead of the
    tree, and an inherited pipe makes it block until the timeout - the first attempt at this ladder
    spent 29 minutes doing that. The process gets its own session so a timeout kills the pipeline
    rather than orphaning `rg` behind a dead `head`. And the recorded commands are login shells, so
    HOME points at a scratch directory: no profile, no skills, the lesson from
    runs/linux-agent-20260919.
    """
    started = time.monotonic()
    environment = dict(os.environ, HOME=str(home)) if home else dict(os.environ)
    process = subprocess.Popen(command, shell=True, cwd=root, env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, errors="replace",
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        output, _ = process.communicate()
    return (output or "")[:SHELL_CAP], (time.monotonic() - started) * 1000


class Server:
    """One retrieval-mcp process pinned to one rung."""

    def __init__(self, binary, root, timeout):
        self.stderr = tempfile.TemporaryFile(mode="w+")
        self.client = benchmark.MCP(
            [str(binary), "--root", str(root), "--profile", "D", "--ranker", "lexical",
             "--timeout-seconds", str(timeout)],
            dict(os.environ), root, self.stderr, timeout + 60)
        self.client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                           "clientInfo": {"name": "scale-response", "version": "1"}})
        self.client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool, arguments):
        started = time.monotonic()
        result = self.client.request("tools/call", {"name": tool, "arguments": arguments})
        elapsed = (time.monotonic() - started) * 1000
        payload = json.dumps(result.get("structuredContent") or result.get("content") or "",
                             ensure_ascii=False)
        return result, payload, elapsed

    def close(self):
        self.client.close()
        self.stderr.close()


def rank_of_gold(root, rows, pairs):
    """1-based rank of the first row whose enclosing definition is a gold identity, or None."""
    for position, row in enumerate(rows, start=1):
        identity = study_b.row_identity(Path(root), row)
        if identity in pairs:
            return position
    return None


def fresh_ranking(server, root, questions, limit):
    """What `search_concept` returns for the raw question at this rung."""
    cells = {}
    for task in questions:
        result, payload, elapsed = server.call(
            "search_concept", {"query": task["question"], "limit": limit})
        rows = [{"path": hit.get("path"), "start_line": hit.get("start_line"),
                 "end_line": hit.get("end_line")}
                for hit in (result.get("structuredContent") or {}).get("results", [])]
        pairs = gold_pairs(task)
        cells[task["id"]] = {
            "rank": rank_of_gold(root, rows, pairs),
            "rows": len(rows),
            "bytes": len(payload.encode()),
            "latency_ms": round(elapsed, 1),
            "gold_visible": len(visible(payload, pairs)),
            "gold_identities": len(pairs),
        }
    return cells


def replay(server, root, questions, calls, timeout, home):
    """The archived calls, re-executed here. Same actions, different corpus."""
    shell, structural = {}, {}
    for task in questions:
        pairs = gold_pairs(task)
        recorded = calls.get(task["id"], {})
        shell_bytes = shell_hits = 0
        for entry in recorded.get("shell", []):
            output, _ = replay_shell(entry["command"], root, timeout, home)
            shell_bytes += len(output.encode())
            shell_hits += bool(visible(entry["command"] + "\n" + output, pairs))
        structural_bytes = structural_hits = 0
        for entry in recorded.get("mcp", []):
            if not entry["tool"]:
                continue
            try:
                _, payload, _ = server.call(entry["tool"], entry["arguments"])
            except RuntimeError:
                payload = ""
            structural_bytes += len(payload.encode())
            structural_hits += bool(visible(json.dumps(entry["arguments"]) + payload, pairs))
        shell[task["id"]] = {"calls": len(recorded.get("shell", [])), "bytes": shell_bytes,
                             "calls_with_gold": shell_hits}
        structural[task["id"]] = {"calls": len(recorded.get("mcp", [])), "bytes": structural_bytes,
                                  "calls_with_gold": structural_hits}
    return shell, structural


def summarize(cells, keys):
    out = {}
    for key in keys:
        values = [cell[key] for cell in cells.values() if cell.get(key) is not None]
        out[key] = {"median": statistics.median(values) if values else None,
                    "mean": round(statistics.fmean(values), 1) if values else None,
                    "n": len(values)}
    return out


def report(args):
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    calls = recorded_calls(args.replay_run) if args.replay_run else {}
    rungs = {}
    home = Path(tempfile.mkdtemp(prefix="scale-response-home-"))
    for spec in args.rungs:
        name, _, path = spec.partition("=")
        root = Path(path).resolve(strict=True)
        files = sum(len(names) for _, _, names in os.walk(root))
        server = Server(args.server, root, args.timeout)
        try:
            fresh = fresh_ranking(server, root, questions, args.limit)
            shell, structural = replay(server, root, questions, calls, args.timeout,
                                       home) if calls else ({}, {})
        finally:
            server.close()
        found = [cell["rank"] for cell in fresh.values() if cell["rank"] is not None]
        rungs[name] = {
            "root": str(root),
            "files": files,
            "fresh": {"cells": fresh,
                      "found_at_all": len(found),
                      "questions": len(questions),
                      **summarize(fresh, ("rank", "bytes", "latency_ms"))},
            "replay_shell": summarize(shell, ("calls", "bytes", "calls_with_gold")) if shell else None,
            "replay_structural": summarize(structural, ("calls", "bytes", "calls_with_gold"))
            if structural else None,
        }
    return {"version": "scale-response-v1", "questions": len(questions),
            "limit": args.limit, "replay_run": str(args.replay_run) if args.replay_run else None,
            "rungs": rungs,
            "limitations":
                "Rungs are nested and every gold file is present in the smallest, so a question is "
                "answerable at every size; what grows is the distractor set. Replayed shell output "
                f"is capped at {SHELL_CAP} bytes, the same cap Codex records, and the share that "
                "reaches a model's context is smaller still. Replay holds behaviour fixed on "
                "purpose: an agent at 928 files would not write the commands it wrote at 86,605, "
                "so replay measures the cost of an action, not the cost of a session. Fresh "
                "ranking queries the raw question text, which understates every surface an agent "
                "would drive with candidate identifiers."}


def render(result):
    lines = [f"{'rung':>12} {'files':>7} {'found':>7} {'rank~':>6} {'bytes~':>8} {'ms~':>7}"
             f" | {'shell B~':>10} {'shell hit':>9} | {'mcp B~':>8} {'mcp hit':>7}"]
    for name, rung in result["rungs"].items():
        fresh = rung["fresh"]
        shell = rung["replay_shell"] or {}
        structural = rung["replay_structural"] or {}
        lines.append(
            f"{name:>12} {rung['files']:>7} {fresh['found_at_all']:>3}/{fresh['questions']:<3}"
            f" {str(fresh['rank']['median']):>6} {str(fresh['bytes']['median']):>8}"
            f" {str(fresh['latency_ms']['median']):>7}"
            f" | {str((shell.get('bytes') or {}).get('median')):>10}"
            f" {str((shell.get('calls_with_gold') or {}).get('mean')):>9}"
            f" | {str((structural.get('bytes') or {}).get('median')):>8}"
            f" {str((structural.get('calls_with_gold') or {}).get('mean')):>7}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--rungs", nargs="+", required=True, help="name=path, smallest first")
    parser.add_argument("--replay-run", type=Path,
                        help="archived comparison run whose calls are replayed verbatim")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.server = args.server.resolve(strict=True)
    result = report(args)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, result)
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
