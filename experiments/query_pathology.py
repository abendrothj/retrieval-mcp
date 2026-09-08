#!/usr/bin/env python3
"""Classify retrieval calls in completed runs from server logs alone; no model calls, no reruns.

Labels describe the observable form and outcome of each call, not model intent. Counting a call
as empty says the query returned no evidence, not that a better query existed.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path


def labels(tool, arguments, result_count, error):
    """Observable properties of one call; a call can carry several labels."""
    found = []
    query = arguments.get("query") if isinstance(arguments, dict) else None
    if error:
        found.append("error")
    elif result_count == 0:
        found.append("empty")
    if tool == "search_exact" and isinstance(query, str):
        regex = arguments.get("regex") is True
        if "\\|" in query:
            # Escaped in ripgrep regex means a literal pipe; with regex off it is two literal chars.
            found.append("escaped_pipe_regex" if regex else "escaped_pipe_literal")
        if "|" in query and len([p for p in query.replace("\\|", "|").split("|") if p.strip()]) > 1:
            found.append("alternation_shaped")
        if not regex and " " in query.strip():
            # A literal multi-word query only matches when that exact phrase sits on one line.
            found.append("literal_multiword")
    return found


def analyze_run(directory):
    events = [json.loads(line) for line in (directory/"server.jsonl").read_text().splitlines()] \
        if (directory/"server.jsonl").exists() else []
    run = json.loads((directory/"run.json").read_text())
    paged = {(e["tool"], json.dumps(e.get("arguments", {}).get("query"), sort_keys=True))
             for e in events if e["event"] == "tool_start" and (e.get("arguments") or {}).get("offset")}
    calls = []
    for event in events:
        if event["event"] != "tool_end":
            continue
        arguments = event.get("arguments") or {}
        found = labels(event["tool"], arguments, event.get("result_count"), event.get("error"))
        limit = arguments.get("limit")
        if event.get("result_count") is not None and limit is not None and event["result_count"] >= limit:
            found.append("page_followed" if (event["tool"], json.dumps(arguments.get("query"), sort_keys=True))
                         in paged else "page_truncated_not_followed")
        calls.append({"tool":event["tool"], "arguments":arguments, "result_count":event.get("result_count"),
                      "retrieval_bytes":event.get("retrieval_bytes"), "labels":found})
    # Both harnesses are readable: the v1 runner names the profile, the factor runner names a cell.
    factors = run.get("condition_factors") or {}
    return {"run_id":run.get("run_id") or run.get("condition"), "task_id":run["task_id"],
            "profile":run.get("profile") or factors.get("availability"),
            "condition":run.get("condition") or run.get("profile"),
            "trial":run.get("trial") or run.get("repetition"), "status":run["status"], "correct":run.get("correct"),
            "format_correct":run.get("format_correct"), "calls":calls,
            "call_count":len(calls), "empty_calls":sum("empty" in c["labels"] for c in calls),
            "label_counts":dict(Counter(label for c in calls for label in c["labels"]))}


def report(directory):
    trials = [record.parent for record in directory.rglob("run.json") if (record.parent/"server.jsonl").exists()]
    runs = sorted((analyze_run(trial) for trial in trials), key=lambda r: r["run_id"])
    profiles = {}
    for run in runs:
        row = profiles.setdefault(run["condition"], {"runs":0, "calls":0, "empty_calls":0, "labels":Counter()})
        row["runs"] += 1
        row["calls"] += run["call_count"]
        row["empty_calls"] += run["empty_calls"]
        row["labels"].update(run["label_counts"])
    for row in profiles.values():
        row["empty_share"] = row["empty_calls"]/row["calls"] if row["calls"] else None
        row["labels"] = dict(row["labels"])
    index = {(r["task_id"], r["trial"], r["condition"]):r for r in runs}
    pairs = []
    for (task, trial, condition), run in sorted(index.items()):
        if condition != "A" or (task, trial, "D") not in index:
            continue
        other = index[(task, trial, "D")]
        if run["status"] != "completed" or other["status"] != "completed":
            continue
        # Bookkeeping only: deleting empty calls does not simulate what the model would have done next.
        pairs.append({"task_id":task, "trial":trial,
            "a_calls":run["call_count"], "d_calls":other["call_count"],
            "a_empty":run["empty_calls"], "d_empty":other["empty_calls"],
            "excess":run["call_count"] - other["call_count"],
            "excess_excluding_empty":(run["call_count"] - run["empty_calls"]) - (other["call_count"] - other["empty_calls"])})
    by_task = defaultdict(lambda: {"excess":0, "excess_excluding_empty":0, "pairs":0})
    for pair in pairs:
        row = by_task[pair["task_id"]]
        row["pairs"] += 1
        row["excess"] += pair["excess"]
        row["excess_excluding_empty"] += pair["excess_excluding_empty"]
    return {"version":"query-pathology-v1", "runs_analyzed":len(runs), "profiles":profiles,
            "a_vs_d_pairs":pairs, "a_vs_d_by_task":dict(by_task), "runs":runs,
            "limitations":"Labels are observable call properties, not intent or error rates. Empty means no "
                          "evidence returned. Excess-excluding-empty is arithmetic bookkeeping, not a "
                          "counterfactual: a model without those calls would have made different ones. "
                          "Retrospective and unblinded; the interventions were designed after seeing these runs."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.directory)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    for profile, row in sorted(result["profiles"].items()):
        print(f"{profile}: {row['runs']:3d} runs {row['calls']:4d} calls "
              f"{row['empty_calls']:3d} empty ({row['empty_share']:.1%}) {row['labels']}")
    total = sum(p["excess"] for p in result["a_vs_d_pairs"])
    residual = sum(p["excess_excluding_empty"] for p in result["a_vs_d_pairs"])
    print(f"A-D matched pairs: {len(result['a_vs_d_pairs'])}, net excess calls {total}, "
          f"excluding empty calls {residual}")


if __name__ == "__main__":
    main()
