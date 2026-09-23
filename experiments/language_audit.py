#!/usr/bin/env python3
"""Audit one language's structural rows against an independent reading of the corpus. No model.

A new grammar is easy to add and easy to believe. The TypeScript member-call defect is what this
script exists to catch: the index parsed the corpus, answered every question with a confident
shape, and silently dropped every `this.method()` call site - 14 real call sites of `getEdits`
reported as none - while free functions looked perfectly healthy. Nothing in the server's own
output said so, because the server cannot know what it failed to see.

So the server is asked, and the corpus is read separately:

  definitions - every name the oracle finds defined in one file only must be found by
                `find_symbol` in that same file. A name the server cannot define is a construct
                its node list does not know.
  callers     - for each sampled helper, `find_callers` rows are compared with
                `audit_failures.true_callers`, which enumerates call sites by ripgrep and
                attributes each to its enclosing definition by reading the file. Precision is
                what the server claims and the corpus does not show; recall is what the corpus
                shows and the server missed.
  attribution - optionally, the oracle itself is cross-checked against universal-ctags, which
                parses independently and reports each definition's line range, so `enclosing`
                is audited rather than trusted. ctags emits end lines for C, C++, Go, Java and
                Python but not for Rust or JavaScript/TypeScript, which is why it is a
                cross-check and not the oracle.

Disagreement is not automatically a server defect: the oracle is a reader too. Every row this
prints is a claim to check against source, which is the point - the numbers say where to look.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile

import audit_failures
import benchmark
import quality_pass

# One sampling population per language family, matching what the server treats as one language.
FAMILIES = {
    "rust": (".rs",),
    "python": (".py",),
    "ecmascript": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
    "go": (".go",),
    "java": (".java",),
    "c": (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
}
# ctags language names for the cross-check, for the suffixes where it reports end lines.
CTAGS_LANGUAGE = {".go": "Go", ".java": "Java", ".py": "Python", ".c": "C", ".h": "C++",
                  ".cc": "C++", ".cpp": "C++", ".cxx": "C++", ".hh": "C++", ".hpp": "C++",
                  ".hxx": "C++"}
CALL_LINE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")


def fingerprint(corpus, suffixes):
    """Files and bytes in scope, so a result names the corpus it was measured on."""
    files = sorted(path for path in Path(corpus).rglob("*")
                   if path.is_file() and path.suffix in suffixes)
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(corpus)).encode())
        digest.update(str(path.stat().st_size).encode())
    return {"files": len(files), "sha256": digest.hexdigest()}


def sample_symbols(corpus, suffixes, index, wanted, seed):
    """Names defined exactly once in the language under audit, that something else also calls."""
    unique = sorted(name for name, paths in index.items()
                    if len(paths) == 1 and Path(next(iter(paths))).suffix in suffixes
                    and len(name) > 2)
    random.Random(seed).shuffle(unique)
    globs = []
    for suffix in suffixes:
        globs += ["-g", f"*{suffix}"]
    chosen = []
    for name in unique:
        if len(chosen) >= wanted:
            break
        defining = next(iter(index[name]))
        found = subprocess.run(
            ["rg", "--no-config", "-l", rf"\b{re.escape(name)}\s*\(", *globs, "."],
            cwd=corpus, capture_output=True, text=True, timeout=120)
        elsewhere = {line.lstrip("./") for line in found.stdout.splitlines()} - {defining}
        if elsewhere:
            chosen.append((name, defining))
    return chosen


def definition_line(corpus, relative, name):
    """The first line of the defining file that the oracle would read as defining `name`."""
    source = (Path(corpus) / relative).read_text(encoding="utf-8", errors="ignore").splitlines()
    for text in source:
        for expression in (quality_pass.DEFINITION, quality_pass.TS_DEFINITION,
                           quality_pass.GO_DEFINITION, quality_pass.JAVA_DEFINITION,
                           quality_pass.C_DEFINITION):
            match = expression.match(text)
            if match and next((group for group in match.groups() if group), None) == name:
                return text.strip()[:160]
    return None


def is_plain_call(row):
    """Whether a `find_callers` row is a call site.

    The payload omits `kind` when it is the default - `src/index/mod.rs` says so in as many
    words, and the diet that introduced it is in `runs/linux-diet-20260921`. This audit went on
    testing `kind == "call"` afterwards, so every plain call row was silently dropped and the
    audit reported `caller_recall: 0.0` against a working index for *every* language: Java on
    gson showed 0 server rows against the oracle's 342, and Python on Django 0 against 61, while
    a direct probe returned 100 rows with 150 incoming callers and correct attribution. An absent
    kind therefore means a plain call, which is what the server's own `is_plain_call` means by it.
    """
    kind = row.get("kind")
    return kind is None or kind == "call"


def server_rows(client, name, defining):
    """`find_callers` call rows and `find_symbol` definitions for one name.

    Rows inside the helper's own defining file are dropped, because the oracle excludes them by
    the same convention every caller gold in this repository uses (`audit_failures.true_callers`).
    Comparing them would score a documented difference in scope as a defect.
    """
    # Every page, not the first: a helper with hundreds of call sites would otherwise read as a
    # recall failure, because the server told the truth and the audit only asked once.
    rows, offset, payload = set(), 0, {}
    while True:
        callers = client.request("tools/call", {"name": "find_callers", "arguments": {
            "name": name, "limit": 100, "offset": offset}})
        if callers.get("isError"):
            raise RuntimeError(f"find_callers failed for {name}: {callers}")
        payload = callers.get("structuredContent") or {}
        results = payload.get("results", [])
        rows |= {f"{row['path']}::{row['caller']}" for row in results
                 if is_plain_call(row) and row.get("caller") and row["path"] != defining}
        if not payload.get("has_more") or payload.get("next_offset") is None:
            break
        offset = payload["next_offset"]
    defined = client.request("tools/call", {"name": "find_symbol", "arguments": {
        "name": name, "limit": 100}})
    if defined.get("isError"):
        raise RuntimeError(f"find_symbol failed for {name}: {defined}")
    definitions = {row["path"] for row in
                   (defined.get("structuredContent") or {}).get("results", [])}
    return rows, definitions, (payload.get("coverage") or {})


def ctags_frames(path):
    """Every (name, first line, last line) universal-ctags reports for one file."""
    language = CTAGS_LANGUAGE.get(path.suffix)
    if language is None:
        return None
    found = subprocess.run(["ctags", "--output-format=json", "--fields=*", "-f", "-",
                            f"--languages={language}", str(path)],
                           capture_output=True, text=True, timeout=60)
    frames = []
    for line in found.stdout.splitlines():
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tag.get("_type") == "tag" and tag.get("end"):
            frames.append((tag["name"].split(".")[-1].split("::")[-1], tag["line"], tag["end"]))
    return frames


def cross_check(corpus, suffixes, sample, seed):
    """Agreement between the oracle's attribution and universal-ctags, where ctags has ranges."""
    if subprocess.run(["ctags", "--version"], capture_output=True).returncode != 0:
        return {"available": False}
    files = [path for path in Path(corpus).rglob("*")
             if path.is_file() and path.suffix in suffixes and path.suffix in CTAGS_LANGUAGE]
    random.Random(seed).shuffle(files)
    agree, disagree, examples = 0, 0, []
    for path in files:
        if agree + disagree >= sample:
            break
        frames = ctags_frames(path) or []
        if not frames:
            continue
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for number, text in enumerate(lines, 1):
            if agree + disagree >= sample:
                break
            # Declaration lines are excluded by construction: the oracle attributes a header to
            # the frame around it, ctags attributes it to the frame it opens, and neither is
            # wrong. Only lines inside a body are comparable.
            if not CALL_LINE.search(text) or text.strip().startswith(("//", "#", "*", "/*")):
                continue
            if audit_failures.declared_name(text, path.suffix) is not None:
                continue
            mine = audit_failures.enclosing(path, number)
            containing = [frame for frame in frames if frame[1] <= number <= frame[2]]
            theirs = min(containing, key=lambda f: f[2] - f[1])[0] if containing else None
            if mine == theirs:
                agree += 1
            else:
                disagree += 1
                if len(examples) < 20:
                    examples.append({"path": str(path.relative_to(corpus)), "line": number,
                                     "source": text.strip()[:120], "oracle": mine,
                                     "ctags": theirs})
    return {"available": True, "compared": agree + disagree, "agree": agree,
            "disagree": disagree, "examples": examples}


def audit(corpus, command, family, wanted, seed, do_cross_check):
    suffixes = FAMILIES[family]
    index = quality_pass.definitions(corpus)
    sample = sample_symbols(corpus, suffixes, index, wanted, seed)
    stderr = tempfile.TemporaryFile(mode="w+")
    client = benchmark.MCP(command, None, corpus, stderr, 300)
    findings, coverage = [], {}
    totals = Counter()
    try:
        client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "language-audit", "version": "1"}})
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        advertised = {tool["name"] for tool in client.request("tools/list", {})["tools"]}
        if not {"find_callers", "find_symbol"} <= advertised:
            raise RuntimeError("server must expose find_callers and find_symbol for this audit")
        for name, defining in sample:
            rows, definitions, coverage = server_rows(client, name, defining)
            expected = set(audit_failures.true_callers(corpus, name, defining))
            claimed_only = sorted(rows - expected)
            missed = sorted(expected - rows)
            totals["symbols"] += 1
            totals["server_rows"] += len(rows)
            totals["oracle_rows"] += len(expected)
            totals["agreed_rows"] += len(rows & expected)
            totals["definition_found"] += int(defining in definitions)
            findings.append({
                "name": name, "defining_path": defining,
                # The line the oracle read as the definition, because a mismatch is as often the
                # oracle claiming a definition the server deliberately does not index - a plain
                # `const` binding, a struct field - as it is a construct the server missed.
                "definition_line": definition_line(corpus, defining, name),
                "server_rows": len(rows), "oracle_rows": len(expected),
                "agreed": len(rows & expected),
                "server_only": claimed_only[:10], "oracle_only": missed[:10],
                "definition_found": defining in definitions,
                "definition_paths": sorted(definitions)[:5],
            })
    finally:
        client.close()
        stderr.close()
    precision = totals["agreed_rows"] / totals["server_rows"] if totals["server_rows"] else None
    recall = totals["agreed_rows"] / totals["oracle_rows"] if totals["oracle_rows"] else None
    return {
        "version": "language-audit-v1",
        "family": family,
        "corpus": str(corpus),
        "corpus_fingerprint": fingerprint(corpus, suffixes),
        "binary_sha256": hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
        "symbols_audited": totals["symbols"],
        "definitions_found": totals["definition_found"],
        "caller_precision": precision,
        "caller_recall": recall,
        "server_rows": totals["server_rows"],
        "oracle_rows": totals["oracle_rows"],
        "coverage": {key: coverage.get(key) for key in
                     ("indexed_files", "eligible_files", "unsupported_files", "parse_error_files",
                      "skipped_files", "budget_truncated")},
        "attribution_cross_check": cross_check(corpus, suffixes, 150, seed) if do_cross_check
                                   else {"available": False},
        "findings": findings,
        "limitations": "Both sides read syntax only. A disagreement names a line to check, not a "
                       "verdict: the oracle attributes a call to its enclosing definition by "
                       "reading braces or indentation, and it can be wrong too.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--family", choices=sorted(FAMILIES), required=True)
    parser.add_argument("--binary", type=Path, default=Path("target/release/retrieval-mcp"))
    parser.add_argument("--symbols", type=int, default=25)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--cross-check", action="store_true",
                        help="also audit the oracle's attribution against universal-ctags")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    command = [str(args.binary.resolve()), "--root", str(args.corpus.resolve()),
               "--tools", "search_exact,read_source,find_callers,find_symbol"]
    report = audit(args.corpus.resolve(), command, args.family, args.symbols, args.seed,
                   args.cross_check)
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
