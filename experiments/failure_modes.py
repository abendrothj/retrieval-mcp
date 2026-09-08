#!/usr/bin/env python3
"""Split failed trials into the three modes that have different token signatures. No model calls.

never_saw_evidence: the gold lines were never returned or read, though the file may have been.
incomplete:         everything answered was right and something was missing - it stopped early.
misread:            the gold lines were seen and the answer contradicts them.

Priority is line coverage, then completeness, then interpretation, so a trial lands in exactly one.
"""
import argparse
import json
from pathlib import Path

from quality_pass import answer_json, credit, definitions, same


def gold_lines(task, corpus):
    lines = {}
    for evidence in task["evidence"]:
        text = (corpus/evidence["path"]).read_text(encoding="utf-8").splitlines()
        for anchor in evidence["contains"]:
            for number, line in enumerate(text, 1):
                if anchor in line:
                    lines.setdefault(evidence["path"], set()).add(number)
                    break
    return lines


def covered(attempt):
    """Line numbers actually delivered, from what the server returned rather than what was asked."""
    seen = {}
    log = attempt/"server.jsonl"
    if not log.exists():
        return seen
    for record in log.read_text().splitlines():
        event = json.loads(record)
        if event.get("event") != "tool_end":
            continue
        for location in event.get("locations") or []:
            path, start = location.get("path"), location.get("line")
            if not path or start is None:
                continue
            end = location.get("end_line") or start
            seen.setdefault(path, set()).update(range(start, end + 1))
    return seen


def classify(got, gold, seen, wanted, index):
    if not any(seen.get(path, set()) & numbers for path, numbers in wanted.items()):
        return "never_saw_evidence"
    if isinstance(gold, list) and isinstance(got, list):
        wrong = [g for g in got if not any(same(g, expected, index) for expected in gold)]
        if not wrong and len(got) < len(gold):
            return "incomplete"
    if got in (None, [], ""):
        return "incomplete"
    return "misread"


def report(directories, questions_path, corpus):
    corpus = Path(corpus)
    questions = {q["id"]: q for q in json.loads(Path(questions_path).read_text())}
    index = definitions(corpus)
    wanted = {name: gold_lines(task, corpus) for name, task in questions.items()}
    cells = {}
    for directory in directories:
        for trial in sorted(Path(directory).glob("trial-*")):
            attempts = sorted(trial.glob("attempt-*/run.json"))
            record = json.loads(attempts[-1].read_text())
            if record["status"] != "completed" or record["condition"].startswith("N-"):
                continue
            attempt = attempts[-1].parent
            gold = questions[record["task_id"]]["expected_json"]["answer"]
            got = answer_json(record.get("answer"))
            bytes_delivered = 0
            if (attempt/"policy.jsonl").exists():
                bytes_delivered = sum(json.loads(line)["delivered_bytes"]
                                      for line in (attempt/"policy.jsonl").read_text().splitlines())
            row = cells.setdefault(record["condition"], {"trials":0, "correct":0, "modes":{}, "detail":[]})
            row["trials"] += 1
            if credit(got, gold, index) == 1.0:
                row["correct"] += 1
                row["detail"].append({"task_id":record["task_id"], "mode":"correct",
                                      "calls":record["attempted_calls"], "bytes":bytes_delivered})
                continue
            mode = classify(got, gold, covered(attempt), wanted[record["task_id"]], index)
            row["modes"][mode] = row["modes"].get(mode, 0) + 1
            row["detail"].append({"task_id":record["task_id"], "mode":mode,
                                  "calls":record["attempted_calls"], "bytes":bytes_delivered})
    return {"version":"failure-modes-v1", "cells":cells,
            "limitations":"Line coverage is what the server returned, not what the model attended to. "
                          "Anchors mark a few lines per file, so 'saw the evidence' means the anchor line "
                          "was delivered, not that the surrounding span was. One repetition, twelve questions."}


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
    print(f"{'cell':28s} {'right':>6s} {'never saw':>10s} {'incomplete':>11s} {'misread':>8s}")
    for cell, row in sorted(result["cells"].items()):
        m = row["modes"]
        print(f"{cell:28s} {row['correct']:>3d}/{row['trials']:<2d} {m.get('never_saw_evidence',0):>10d}"
              f" {m.get('incomplete',0):>11d} {m.get('misread',0):>8d}")
    print()
    print(f"{'outcome':22s} {'n':>3s} {'median calls':>13s} {'median KB delivered':>20s}")
    from statistics import median
    buckets = {"correct": [], **{m: [] for m in ("never_saw_evidence", "incomplete", "misread")}}
    for row in result["cells"].values():
        for d in row["detail"]:
            buckets[d["mode"]].append(d)
    for name, items in buckets.items():
        if items:
            print(f"{name:22s} {len(items):>3d} {median(i['calls'] for i in items):>13.1f}"
                  f" {median(i['bytes'] for i in items)/1000:>19.1f}K")
