#!/usr/bin/env python3
"""Offline ModelShare reanalysis; preserve original scores and expose paired observations."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from statistics import mean, median

import analyze
from audit_answers import audit_answer


def distribution(values):
    return {"n":len(values), "mean":mean(values) if values else None,
            "median":median(values) if values else None, "sum":sum(values)}


def summarize(pairs):
    fields = ("total_calls_saved", "search_exact_saved", "read_source_saved", "wall_time_ms_saved",
              "retrieval_bytes_saved", "input_tokens_saved", "cache_creation_input_tokens_saved",
              "cache_read_input_tokens_saved")
    result = {}
    for label, selected in (("all_eligible", pairs), ("both_payload_match", [p for p in pairs if p["both_payload_match"]])):
        values = [p["total_calls_saved"] for p in selected]
        result[label] = {"pairs":len(selected), "questions":len({p["task_id"] for p in selected}),
            "fewer_calls":sum(v > 0 for v in values), "equal_calls":values.count(0),
            "more_calls":sum(v < 0 for v in values),
            "saved":{f:distribution([p[f] for p in selected if p.get(f) is not None]) for f in fields}}
    tasks = sorted({p["task_id"] for p in pairs})
    result["leave_one_question_out_calls_saved"] = {
        task:distribution([p["total_calls_saved"] for p in pairs if p["task_id"] != task]) for task in tasks}
    return result


def report(directory):
    question_path = directory/"modelshare-questions.json"
    questions = {q["id"]:q for q in json.loads(question_path.read_text())}
    analysis = analyze.analyze_directory(directory/"modelshare")
    inputs = {str(question_path.relative_to(directory)):hashlib.sha256(question_path.read_bytes()).hexdigest()}
    runs, lookup = [], {}
    for run in analysis["runs"]:
        m = run["metadata"]
        for filename in ("run.json", "server.jsonl", "transcript.jsonl"):
            path = directory/"modelshare"/m["run_id"]/filename
            if path.exists():
                inputs[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
        row = {"run_id":m["run_id"], "task_id":m["task_id"], "category":m["category"],
               "trial":m["trial"], "profile":m["profile"], "eligible":run["eligible_for_comparison"],
               "frozen_correct":m.get("correct"), "payload_audit":audit_answer(questions[m["task_id"]], m.get("answer")),
               "calls":sum(run["tool_counts"].values()), "tool_counts":run["tool_counts"],
               "wall_time_ms":m.get("wall_time_ms"), "usage":m.get("usage", {}),
               "retrieval_bytes":sum(s["retrieval_bytes"] for s in run["sessions"]),
               "sessions":run["sessions"]}
        runs.append(row)
        lookup[(m["task_id"], m["trial"], m["profile"])] = row
    pairs = []
    for comparison in analysis["matched_comparisons"]:
        if not comparison["eligible"]:
            continue
        a = lookup[(comparison["task_id"], comparison["trial"], comparison["baseline"])]
        b = lookup[(comparison["task_id"], comparison["trial"], comparison["variant"])]
        pair = {**comparison, "baseline_run":a["run_id"], "variant_run":b["run_id"],
                "both_payload_match":a["payload_audit"] == b["payload_audit"] == "payload_matches",
                "retrieval_bytes_saved":a["retrieval_bytes"] - b["retrieval_bytes"]}
        for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            x, y = (r["usage"].get(field) for r in (a, b))
            pair[field + "_saved"] = x - y if isinstance(x, (int, float)) and isinstance(y, (int, float)) else None
        pairs.append(pair)
    grouped = defaultdict(list)
    for pair in pairs:
        label = pair["baseline"] + "-" + pair["variant"]
        for scope in ("all", pair["category"], pair["task_id"]):
            grouped[(label, scope)].append(pair)
    profiles = []
    for profile in "ABCD":
        selected = [r for r in runs if r["profile"] == profile and r["eligible"]]
        profiles.append({"profile":profile, "trials":len(selected),
            "payload_matches":sum(r["payload_audit"] == "payload_matches" for r in selected),
            "calls":distribution([r["calls"] for r in selected]),
            "wall_time_ms":distribution([r["wall_time_ms"] for r in selected]),
            "retrieval_bytes":distribution([r["retrieval_bytes"] for r in selected]),
            "first_tools":dict(Counter(s["first_tool"] for r in selected for s in r["sessions"]))})
    return {"version":"modelshare-research-v1", "input_sha256":inputs, "profiles":profiles, "runs":runs,
            "pairs":pairs, "comparisons":[{"contrast":key[0], "scope":key[1], **summarize(value)}
                                            for key,value in sorted(grouped.items())],
            "limitations":"Post-hoc, single repository, 12 questions with 3 repetitions each. Positive saved values favor the variant. All paired observations retained; both-matching subsets are outcome-selected. No significance or causal claim. Source-read overlap is not proof of verification. Input/cache token fields remain separate. Frozen scores unchanged."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.directory)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["profiles"], indent=2))


if __name__ == "__main__":
    main()
