#!/usr/bin/env python3
"""Diagnostic: how much of the strict score is notation rather than retrieval?

This is not a grader. It strips the path spelling from an answer and compares identifiers, which is
deliberately crude: it shows whether the strict measure is dominated by notation, and a replacement
measure has to resolve a name against the corpus and require a unique definition.
"""
import argparse
import json
from pathlib import Path
import re


def tail(symbol):
    if not isinstance(symbol, str):
        return symbol
    parts = [p for p in re.split(r"::|/", symbol) if p and not p.endswith(".rs")]
    if len(parts) > 1 and parts[-2][:1].isupper():
        return "::".join(parts[-2:])
    return parts[-1] if parts else symbol


def normalise(value):
    if isinstance(value, list):
        return sorted(tail(v) for v in value)
    if isinstance(value, dict):
        return {k: tail(v) if isinstance(v, str) else v for k, v in value.items()}
    return tail(value)


def answer_json(text):
    for block in re.findall(r"```(?:json)?[ \t]*\n(.*?)\n?```", text or "", re.DOTALL):
        try:
            parsed = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(parsed, dict) and set(parsed) == {"answer"}:
            return parsed["answer"]
    return None


def report(directories, questions):
    gold = {q["id"]: q["expected_json"]["answer"] for q in json.loads(Path(questions).read_text())}
    cells = {}
    for directory in directories:
        for trial in sorted(Path(directory).glob("trial-*")):
            records = sorted(trial.glob("attempt-*/run.json"))
            record = json.loads(records[-1].read_text())
            if record["status"] != "completed":
                continue
            got = answer_json(record.get("answer") or "")
            strict = record["payload_matches"] is True
            row = cells.setdefault(record["condition"], {"trials":0, "strict":0, "symbol_resolved":0, "notation_only":[]})
            row["trials"] += 1
            row["strict"] += strict
            resolved = strict or (got is not None and normalise(got) == normalise(gold[record["task_id"]]))
            row["symbol_resolved"] += resolved
            if resolved and not strict:
                row["notation_only"].append(record["task_id"])
    return {"version":"notation-diagnostic-v1", "cells":cells,
            "caveat":"Tail matching is a diagnostic heuristic, not a scoring rule. A replacement measure "
                     "must resolve each name against the pinned corpus and require a unique definition."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+")
    parser.add_argument("--questions", default="experiments/v2_questions_draft.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args.directories, args.questions)
    if args.output:
        with args.output.open("x") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    for cell, row in sorted(result["cells"].items()):
        print(f"{cell:28s} strict {row['strict']:>2d}/{row['trials']:<3d} symbol-resolved "
              f"{row['symbol_resolved']:>2d}/{row['trials']:<3d}  notation-only: {row['notation_only']}")
