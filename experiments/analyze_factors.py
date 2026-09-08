#!/usr/bin/env python3
"""Summarize factor attempts without pooling retries or treating missing trials as successes."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean, median


def stats(values):
    return {"n":len(values), "mean":mean(values) if values else None, "median":median(values) if values else None}


def report(directory):
    manifest = json.loads((directory/"manifest.json").read_text())
    plan = json.loads((directory/"plan.json").read_text())
    trials = []
    for index, trial in enumerate(plan["trials"]):
        paths = sorted((directory/f"trial-{index:04d}").glob("attempt-[0-9][0-9][0-9][0-9]"))
        attempts = [json.loads((p/"run.json").read_text()) if (p/"run.json").exists()
                    else {"status":"interrupted_before_record"} for p in paths]
        # First attempt is primary, never replace a failed outcome with a successful retry.
        first = attempts[0] if attempts else {"status":"not_started"}
        trials.append({**trial, "first_attempt":first, "attempt_count":len(attempts),
            "latest_status":attempts[-1]["status"] if attempts else "not_started",
            "eligible":first["status"] == "completed" and first.get("repository_unchanged") is True
                and not first.get("unexpected_tools") and first.get("attempted_calls", 0) > 0})
    conditions = []
    lookup = {}
    for row in trials:
        lookup[(row["task_id"], row["repetition"], row["condition"])] = row
    for cell in plan["conditions"]:
        rows = [r for r in trials if r["condition"] == cell["id"]]
        eligible = [r["first_attempt"] for r in rows if r["eligible"]]
        conditions.append({**cell, "assigned":len(rows), "statuses":dict(Counter(r["first_attempt"]["status"] for r in rows)),
            "eligible_completed":len(eligible), "primary_passes":sum(r["correct"] is True for r in eligible),
            "policy_nonadherent":sum(r.get("policy_adherent") is False for r in eligible),
            "budget_exhaustions":sum(r["first_attempt"].get("budget_exhausted", False) for r in rows),
            "calls_completed":stats([r["attempted_calls"] for r in eligible]),
            "wall_time_ms_completed":stats([r["wall_time_ms"] for r in eligible])})
    pairs = []
    for contrast in plan["contrasts"]:
        for task, repetition in sorted({(r["task_id"], r["repetition"]) for r in trials}):
            a, b = (lookup[(task, repetition, contrast[key])] for key in ("baseline", "variant"))
            eligible = a["eligible"] and b["eligible"]
            pairs.append({**contrast, "task_id":task, "repetition":repetition,
                "baseline_status":a["first_attempt"]["status"], "variant_status":b["first_attempt"]["status"],
                "eligible_completed_pair":eligible,
                "baseline_correct":a["first_attempt"].get("correct"), "variant_correct":b["first_attempt"].get("correct"),
                "calls_saved":a["first_attempt"]["attempted_calls"] - b["first_attempt"]["attempted_calls"] if eligible else None})
    return {"version":"factor-analysis-v1", "test_only":manifest["client"] == "scripted", "conditions":conditions,
            "pairs":pairs, "trials":trials, "limitations":"First attempts are primary; retries never replace them. Policy violations remain assigned to their condition. Missing/failed/contaminated trials are explicit, not zero-call successes. Completed-pair savings are descriptive and selected. Scripted runs do not measure model routing or tokens."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.directory)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["conditions"], indent=2))
