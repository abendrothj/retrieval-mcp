#!/usr/bin/env python3
"""Reject questions whose structural difficulty a bounded lexical crawl dissolves.

`tool_reachability.py` proves a structural route to a gold exists. The schema ablation showed that
is not the property a tool-necessity suite needs: every question in it was also answerable with
`search_concept`/`search_exact` plus `read_source`, so removing structural tools cost nothing and
the suite measured availability rather than necessity.

This is the missing gate, and it is deliberately mechanical - no model, no network. It runs a
strong but bounded lexical agent over one question:

  * it starts only from the question text, so information must flow through retrieval;
  * it may never query a gold identifier directly, only identifiers it has actually discovered;
  * it harvests definition names and paths from every result and follows them;
  * it stops the moment every gold identity is visible in what tools returned.

Nothing here proves lexical search *cannot* find a symbol - given enough calls it crawls the whole
repository. The claim is operational and budgeted: within the same call budget the lexical-only
treatment arm receives, this crawl did or did not recover the complete gold. A question it solves
is not evidence about structural tools and must not enter a necessity suite.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import tempfile

import benchmark
import quality_pass

TOOLS = ("search_exact", "search_concept", "read_source")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
DEFINITION = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
ATTRIBUTE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]{3,})\s*\(")
# Question prose is full of words that are also plausible identifiers; querying them wastes the
# budget on English rather than on code, which would make the oracle look artificially weak.
PROSE = {
    "that", "this", "which", "with", "when", "where", "from", "into", "each", "every", "returns",
    "return", "identify", "answer", "question", "object", "objects", "method", "methods", "class",
    "classes", "function", "functions", "definition", "definitions", "symbol", "symbols", "name",
    "names", "path", "value", "values", "list", "json", "call", "calls", "called", "caller",
    "callers", "module", "modules", "package", "packages", "field", "fields", "model", "models",
    "first", "second", "other", "same", "only", "their", "there", "those", "these", "through",
    "without", "within", "before", "after", "then", "than", "must", "does", "have", "been",
    "using", "used", "uses", "make", "made", "over", "some", "such", "both", "while", "whose",
    "what", "given", "line", "lines", "code", "source", "file", "files", "text", "type", "types",
}


def identities(task):
    """Every (path, leaf) the gold asserts."""
    found = []
    for value in quality_pass.flatten(task["expected_json"]["answer"]):
        path, _, rest = value.partition("::")
        if rest:
            found.append((path, rest.split("::")[-1]))
    return found


def exposed(text, pairs):
    """The gold identities visible in what tools returned, judged as end_to_end judges a hit."""
    return [pair for pair in pairs if pair[0] in text and pair[1] in text]


def question_terms(question, forbidden):
    """Identifier-like terms the question itself supplies, best first, minus every gold name."""
    scored = Counter()
    for match in re.findall(r"`([^`]+)`", question):
        for term in IDENTIFIER.findall(match):
            scored[term] += 5
    for term in IDENTIFIER.findall(question):
        lowered = term.lower()
        if lowered in PROSE:
            continue
        # Code-shaped spellings beat prose: underscores and internal capitals are strong signals.
        scored[term] += 3 if ("_" in term or not term.islower()) else 1
    return [term for term, _ in sorted(scored.items(), key=lambda item: (-item[1], item[0]))
            if term not in forbidden]


def harvest(payload):
    """Definition names and attribute calls a result exposes, as the next things worth querying."""
    names, paths = Counter(), Counter()

    def visit(node):
        if isinstance(node, dict):
            if isinstance(node.get("path"), str):
                paths[node["path"]] += 1
            for key, value in node.items():
                if key in ("name", "caller", "callee", "container") and isinstance(value, str):
                    names[value.split("::")[-1]] += 2
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, str):
            for line in node.splitlines():
                match = DEFINITION.match(line)
                if match:
                    names[match.group(1)] += 3
                for attribute in ATTRIBUTE.findall(line):
                    names[attribute] += 1

    visit(payload)
    return names, paths


def hit_lines(payload):
    """(path, line) pairs a lexical result points at, so reads can be aimed rather than blind."""
    found = []

    def visit(node):
        if isinstance(node, dict):
            if isinstance(node.get("path"), str) and isinstance(node.get("line"), int):
                found.append((node["path"], node["line"]))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return found


class Crawl:
    """One question's bounded lexical search, newest evidence first."""

    def __init__(self, task, budget, window):
        self.pairs = identities(task)
        self.forbidden = {leaf for _, leaf in self.pairs}
        self.budget, self.window = budget, window
        self.terms = question_terms(task["question"], self.forbidden)
        self.text, self.trace = "", []
        self.queried, self.read = set(), set()
        self.names, self.paths = Counter(), Counter()
        self.reads = []
        self.concept_query = task["question"]

    def solved(self):
        return len(exposed(self.text, self.pairs)) == len(self.pairs) and bool(self.pairs)

    def next_action(self, step):
        """The strongest untried lexical move, in a fixed order so the gate is reproducible."""
        if step == 0:
            return "search_concept", {"query": self.concept_query, "limit": 10}
        for term in self.terms:
            if term not in self.queried:
                self.queried.add(term)
                return "search_exact", {"query": term, "limit": 10}
        # Read what the searches pointed at, widest-support file first, around the matched line.
        for path, line in self.reads:
            key = (path, line // self.window)
            if key in self.read:
                continue
            self.read.add(key)
            start = max(1, line - self.window // 2)
            return "read_source", {"path": path, "start_line": start,
                                   "end_line": start + self.window}
        for name, _ in self.names.most_common():
            if name not in self.queried and len(name) > 3:
                self.queried.add(name)
                return "search_exact", {"query": name, "limit": 10}
        return None

    def absorb(self, tool, arguments, payload):
        self.text += "\n" + json.dumps(payload, ensure_ascii=False)
        names, paths = harvest(payload)
        self.names.update(names)
        self.paths.update(paths)
        # Newly discovered identifiers are legitimate: the crawl found them, it was not told them.
        self.terms.extend(name for name, _ in names.most_common(3)
                          if name not in self.queried and name not in self.terms)
        for path, line in hit_lines(payload):
            if (path, line) not in self.reads:
                self.reads.append((path, line))
        self.reads.sort(key=lambda item: -self.paths[item[0]])
        self.trace.append({"tool": tool, "arguments": arguments,
                           "exposed": ["::".join(pair) for pair in exposed(self.text, self.pairs)]})


def run(questions, command, corpus, environment, budget, window, timeout):
    stderr = tempfile.TemporaryFile(mode="w+")
    client = benchmark.MCP(command, environment, corpus, stderr, timeout)
    findings, solved_ids = {}, []
    try:
        client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "lexical-oracle", "version": "1"}})
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        advertised = {tool["name"] for tool in client.request("tools/list", {})["tools"]}
        if not set(TOOLS) <= advertised:
            raise RuntimeError(f"server must expose {TOOLS}, advertised {sorted(advertised)}")
        for task in questions:
            crawl = Crawl(task, budget, window)
            for step in range(budget):
                action = crawl.next_action(step)
                if action is None:
                    break
                tool, arguments = action
                result = client.request("tools/call", {"name": tool, "arguments": arguments})
                if result.get("isError"):
                    crawl.trace.append({"tool": tool, "arguments": arguments, "error": True})
                    continue
                crawl.absorb(tool, arguments, result.get("structuredContent") or result)
                if crawl.solved():
                    break
            found = exposed(crawl.text, crawl.pairs)
            findings[task["id"]] = {
                "solved": crawl.solved(),
                "calls_used": len(crawl.trace),
                "gold_identities": len(crawl.pairs),
                "exposed_identities": len(found),
                "trace": [{"tool": entry["tool"], "arguments": entry["arguments"],
                           "exposed": len(entry.get("exposed", []))} for entry in crawl.trace],
            }
            if crawl.solved():
                solved_ids.append(task["id"])
    finally:
        client.close()
        stderr.close()
    return {
        "version": "lexical-oracle-v1",
        "budget": budget,
        "read_window": window,
        "questions": len(questions),
        "solved_by_lexical_crawl": len(solved_ids),
        "resistant": len(questions) - len(solved_ids),
        "solved_ids": solved_ids,
        "findings": findings,
        "limitations": (
            "Operational, not a proof: a larger budget crawls further and would solve more. The "
            "crawl is one fixed deterministic strategy, so a question it fails may still be easy "
            "for a model, which is why a lexical-only model arm remains the second gate. Exposure "
            "is textual visibility of a gold path and leaf, matching end_to_end's first-hit rule."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--semantic-command", type=benchmark.command_array, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--budget", type=int, default=8)
    parser.add_argument("--read-window", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-resistant", action="store_true",
                        help="exit nonzero when any question falls to the lexical crawl")
    args = parser.parse_args()
    questions = json.loads(args.questions.resolve(strict=True).read_text(encoding="utf-8"))
    corpus = args.corpus.resolve(strict=True)
    command = [str(args.server.resolve(strict=True)), "--root", str(corpus), "--profile", "D",
               "--semantic-command", json.dumps(args.semantic_command),
               "--timeout-seconds", str(args.timeout)]
    environment = dict(os.environ)
    environment["RETRIEVAL_SEMANTIC_CACHE_DIR"] = str(args.cache.resolve())
    report = run(questions, command, corpus, environment, args.budget, args.read_window, args.timeout)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, report)
    print(json.dumps({key: report[key] for key in
                      ("budget", "questions", "solved_by_lexical_crawl", "resistant")}, indent=2))
    for task_id in report["solved_ids"]:
        finding = report["findings"][task_id]
        print(f"  - lexical crawl solved {task_id} in {finding['calls_used']} calls")
    return 1 if args.require_resistant and report["solved_ids"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
