#!/usr/bin/env python3
"""Graded quality measures over completed trials. No model calls, no reruns.

Four axes the strict path-string score cannot express: whether a written symbol resolves to the gold
definition however it was spelled, how close a partly-right set or chain came, whether the evidence
was ever retrieved at all, and whether a wrong answer was declined or invented. Symbol resolution
uses ripgrep over the pinned corpus, never the structural index under test.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

DEFINITION = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+|const\s+|async\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*"
                        r"fn\s+([A-Za-z0-9_]+)|^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z0-9_]+)")
# TypeScript declares the same things in more ways than Rust or Python do; each alternative
# names exactly one definition, and methods are matched only where a body follows.
TS_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?:class|interface|enum|type)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:declare\s+)?const\s+([A-Za-z_$][\w$]*)\s*[:=]"
    # A method may carry several modifiers: `private async request(`. Requiring exactly one
    # dropped every such method from the index, and with it the golds that named them.
    r"|^\s*(?:(?:public|private|protected|static|readonly|abstract|override|async)\s+)+\*?\s*"
    r"([A-Za-z_$][\w$]*)\s*[(<]")
SOURCE_SUFFIXES = (".rs", ".py", ".ts", ".tsx")
PATH_TOKEN = re.compile(r"[\w./$-]+\.(?:rs|py|ts|tsx)\b")
IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
DECLINE = re.compile(r"\b(cannot|can't|could not|couldn't|unable|do not have|don't have|no reliable|not able)\b", re.I)


def definitions(corpus):
    """identifier -> set of paths that define it, from source text alone.

    Independent of every system under test: ripgrep over the corpus, no index consulted.
    """
    index = {}
    passes = (
        ([r"^\s*(pub\s+)?(async\s+)?fn\s+[A-Za-z0-9_]+",
          r"^\s*(async\s+)?(def|class)\s+[A-Za-z0-9_]+"],
         ["-g", "*.rs", "-g", "*.py"], DEFINITION),
        ([r"^\s*(export\s+)?(default\s+)?(declare\s+)?(abstract\s+)?(class|interface|enum|type)\s+[A-Za-z_$]",
          r"^\s*(export\s+)?(default\s+)?(declare\s+)?(async\s+)?function\s*\*?\s*[A-Za-z_$]",
          r"^\s*(export\s+)?(declare\s+)?const\s+[A-Za-z_$][\w$]*\s*[:=]",
          r"^\s*((public|private|protected|static|readonly|abstract|override|async)\s+)+\*?\s*[A-Za-z_$][\w$]*\s*[(<]"],
         ["-g", "*.ts", "-g", "*.tsx"], TS_DEFINITION),
    )
    for patterns, globs, expression in passes:
        command = ["rg", "--no-config", "-n", "--no-heading"]
        for pattern in patterns:
            command += ["-e", pattern]
        found = subprocess.run(command + globs + ["."], cwd=corpus,
                               capture_output=True, text=True, timeout=300)
        for line in found.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            match = expression.match(parts[2])
            if match:
                name = next(group for group in match.groups() if group)
                index.setdefault(name, set()).add(parts[0].lstrip("./"))
    return index


def context_segments(written):
    """The identifier-like pieces of a written symbol: `::`, `:`, `/` and `.` all separate them.

    A source-file chunk is location, not a name, so `base.py` contributes nothing; a dotted owner
    such as `Model.from_db` or a dotted module such as `django.apps.config` contributes each
    piece. A single colon is how editors and grep write the same thing - `query.py:Query.combine`
    names one definition exactly as `query.py::Query.combine` does, and a trailing line number
    such as `query.py:1234` is not an identifier, so it still fails to parse rather than
    resolving to something wrong.
    """
    segments = []
    for chunk in re.split(r"::|:|/", written.strip()):
        if not chunk or chunk.endswith(SOURCE_SUFFIXES):
            continue
        segments.extend(piece for piece in chunk.split(".") if piece)
    return segments


def parse_symbol(written, index=None):
    """(type, name) from any spelling: a path, a module path, a bare name, or one prose clause.

    With an index, an answer that reads `getResolvedShellEnv in src/.../shellEnv.ts` is parsed by
    keeping the identifiers that are actually defined somewhere in the corpus. A clause naming two
    unrelated definitions stays ambiguous and is not parsed, so prose cannot win by listing names.

    A dot separates an owner from its member exactly as `::` does: `base.py::Model.from_db` and
    `django.apps.config::AppConfig.create` are the spellings a Python reader writes, and scoring
    them zero measures notation rather than retrieval.
    """
    if not isinstance(written, str):
        return None
    parts = context_segments(written)
    if parts and all(IDENTIFIER.fullmatch(part) for part in parts):
        name = parts[-1]
        owner = parts[-2] if len(parts) > 1 and parts[-2][:1].isupper() else None
        return owner, name
    if index is None:
        return None
    # Path segments are location, not the name being claimed; read identifiers from the prose only.
    prose = PATH_TOKEN.sub(" ", written)
    named = [token for token in dict.fromkeys(IDENTIFIER.findall(prose)) if token in index]
    if len(named) > 1:
        # The answer's own file reference is the context it supplied; use it to pick the claim.
        mentioned = {match.group(0).lstrip("./") for match in PATH_TOKEN.finditer(written)}
        named = [token for token in named if index[token] & mentioned] if mentioned else named
    if len(named) != 1:
        return None
    return None, named[0]


def resolve(written, index):
    """The unique defining path for a written symbol, or None when absent or still ambiguous.

    A name defined in several files is disambiguated by the rest of what was written - a crate,
    module, directory or file segment - which is the context the answer actually supplied.
    Segments are matched against the path mechanically; nothing about the gold answer is consulted.
    """
    parsed = parse_symbol(written, index)
    if not parsed:
        return None
    paths = index.get(parsed[1], set())
    if len(paths) > 1:
        mentioned = {match.group(0).lstrip("./") for match in PATH_TOKEN.finditer(written)}
        narrowed = {path for path in paths if path in mentioned} if mentioned else set()
        if len(narrowed) != 1:
            segments = [s.lower().removeprefix("uu_") for s in context_segments(written)
                        if s not in (parsed[1], parsed[0])]
            narrowed = {p for p in paths
                        if all(s in p.lower() for s in segments)} if segments else paths
        paths = narrowed if len(narrowed) == 1 else paths
    return (sorted(paths)[0], parsed) if len(paths) == 1 else None


def same(written, gold, index):
    if not isinstance(gold, str):
        return written == gold
    left, right = resolve(written, index), resolve(gold, index)
    if left and right:
        return left[0] == right[0] and left[1][1] == right[1][1] and (
            left[1][0] == right[1][0] or None in (left[1][0], right[1][0]))
    return str(written).strip() == gold


def mentions(written, gold, index):
    """Whether a prose answer names this one gold identity unambiguously.

    A set-valued answer written as a sentence is still an answer. It counts an identity only when
    the name it uses resolves to the gold's definition: a name defined once resolves on its own, a
    name with namesakes needs the answer to supply its file. Prose therefore cannot prove
    exhaustiveness - ask for a JSON list when the question demands every caller.
    """
    if not isinstance(gold, str) or "::" not in gold:
        return False
    path, leaf = gold.split("::", 1)
    leaf = leaf.split("::")[-1]
    text = str(written)
    if leaf not in IDENTIFIER.findall(PATH_TOKEN.sub(" ", text)):
        return False
    defined = index.get(leaf, set())
    if len(defined) > 1:
        return path in {match.group(0).lstrip("./") for match in PATH_TOKEN.finditer(text)}
    return not defined or path in defined


def flatten(gold):
    """Every identity a set- or object-valued gold asserts, in declaration order."""
    if isinstance(gold, str):
        return [gold]
    if isinstance(gold, list):
        return [item for value in gold for item in flatten(value)]
    if isinstance(gold, dict):
        return [item for value in gold.values() for item in flatten(value)]
    return []

def credit(got, gold, index):
    """Graded closeness in [0, 1]; sets by overlap, chains by correct prefix, scalars exact."""
    if isinstance(gold, (list, dict)) and isinstance(got, str):
        # Prose against a set: recall over the asserted identities. Extras are not penalised
        # because a sentence cannot be read as a closed set; require a JSON list to test that.
        expected = flatten(gold)
        return sum(mentions(got, item, index) for item in expected) / len(expected) if expected else 0.0
    if isinstance(gold, list):
        # A keyed object whose values name the asserted identities is a spelling of the same set.
        # Scoring it zero while a free sentence naming the same two symbols scores 1.0 would grade
        # notation rather than retrieval, which is the oldest defect in this project.
        if isinstance(got, dict):
            got = flatten(got)
        if not isinstance(got, list):
            return 0.0
        matched = sum(any(same(g, expected, index) for g in got) for expected in gold)
        extra = max(0, len(got) - len(gold))
        return max(0.0, (matched - extra) / len(gold))
    if isinstance(gold, dict):
        # The mirror of the case above, and the same defect: a gold that wraps one assertion in a
        # key ({"callers": [...]}) asserts exactly what the bare value asserts, so a reply naming
        # the same identities under no key, or under a differently spelled one, is the same answer.
        # Scoring it zero grades the container, not the retrieval. Multi-key golds are excluded:
        # there the key names which fact is being asserted, so dropping it does lose information.
        if not isinstance(got, dict):
            return credit(got, next(iter(gold.values())), index) if len(gold) == 1 else 0.0
        if set(got) != set(gold):
            if len(gold) == 1 and len(got) == 1:
                return credit(next(iter(got.values())), next(iter(gold.values())), index)
            return 0.0
        return sum(credit(got[k], gold[k], index) for k in gold) / len(gold)
    return float(same(got, gold, index)) if gold is not None else float(got is None)


def answer_json(text):
    """The answer payload regardless of the reply envelope.

    The frozen strict grader in benchmark.py judges the envelope: a bare {"answer": ...} object.
    This quality axis must not repeat that judgment, or a correct symbol wrapped in prose scores
    zero and the measure reports formatting discipline instead of retrieval quality. Fenced answer
    blocks win; otherwise the last {"answer": ...} object anywhere in the reply is used.
    """
    text = text or ""
    for block in re.findall(r"```(?:json)?[ \t]*\n(.*?)\n?```", text, re.DOTALL):
        try:
            parsed = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(parsed, dict) and set(parsed) == {"answer"}:
            return parsed["answer"]
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(parsed, dict) and set(parsed) == {"answer"}:
            found = parsed["answer"]
    return found


def evidence_paths(attempt):
    """Every path the retrieval layer returned or read in this attempt."""
    seen = set()
    log = attempt/"server.jsonl"
    if not log.exists():
        return seen
    for line in log.read_text().splitlines():
        event = json.loads(line)
        if event.get("event") != "tool_end":
            continue
        for location in event.get("locations") or []:
            if location.get("path"):
                seen.add(location["path"])
        path = (event.get("arguments") or {}).get("path")
        if path and event.get("tool") == "read_source":
            seen.add(path)
    return seen


def report(directories, questions_path, corpus):
    questions = {q["id"]: q for q in json.loads(Path(questions_path).read_text())}
    index = definitions(Path(corpus))
    cells = {}
    for directory in directories:
        for trial in sorted(Path(directory).glob("trial-*")):
            attempts = sorted(trial.glob("attempt-*/run.json"))
            record = json.loads(attempts[-1].read_text())
            if record["status"] != "completed":
                continue
            task = questions[record["task_id"]]
            gold = task["expected_json"]["answer"]
            got = answer_json(record.get("answer"))
            score = credit(got, gold, index)
            wanted = {e["path"] for e in task["evidence"]}
            retrieved = evidence_paths(attempts[-1].parent)
            row = cells.setdefault(record["condition"], {"trials":0, "strict":0, "resolved":0,
                "credit":0.0, "evidence_hits":0, "declined":0, "fabricated":0, "notation_only":[]})
            row["trials"] += 1
            row["strict"] += record["payload_matches"] is True
            row["resolved"] += score == 1.0
            row["credit"] += score
            row["evidence_hits"] += bool(wanted & retrieved) if retrieved else 0
            if score < 1.0:
                # An empty answer, an explicit null, or a stated inability are all declines;
                # a named symbol that is simply wrong is not.
                empty = got in (None, [], "")
                row["declined" if empty or (got is None and DECLINE.search(record.get("answer") or ""))
                    else "fabricated"] += 1
            if score == 1.0 and record["payload_matches"] is not True:
                row["notation_only"].append(record["task_id"])
    for row in cells.values():
        row["credit"] = round(row["credit"]/row["trials"], 3) if row["trials"] else None
    return {"version":"quality-pass-v1", "cells":cells, "definitions_indexed":len(index),
            "measures":{"strict":"frozen json-answer-v3 payload equality on the path string",
                        "resolved":"identifier resolved to a unique definition in the pinned corpus by ripgrep",
                        "credit":"graded closeness: sets by overlap less extras, dicts per key, scalars exact",
                        "evidence_hits":"a gold evidence file was returned or read at least once",
                        "declined":"wrong and the reply states it cannot answer",
                        "fabricated":"wrong and stated as an answer"},
            "limitations":"Resolution requires a unique definition, so an ambiguous name scores wrong. "
                          "Evidence coverage is file-level, not line-level, and says nothing about whether "
                          "the evidence was read before the claim. Twelve questions, one repetition."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+")
    parser.add_argument("--questions", default="experiments/v2_questions_draft.json")
    parser.add_argument("--corpus", default="../runs/projects-v2-suite/coreutils/corpus")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args.directories, args.questions, args.corpus)
    if args.output:
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    print(f"{'cell':28s} {'strict':>7s} {'resolved':>9s} {'credit':>7s} {'evidence':>9s} {'declined':>9s} {'fabricated':>11s}")
    for cell, row in sorted(result["cells"].items()):
        n = row["trials"]
        print(f"{cell:28s} {row['strict']:>4d}/{n:<2d} {row['resolved']:>6d}/{n:<2d} {row['credit']:>7.2f}"
              f" {row['evidence_hits']:>6d}/{n:<2d} {row['declined']:>9d} {row['fabricated']:>11d}")
