#!/usr/bin/env python3
"""Analyze native retrieval-system trials and matched task comparisons."""
import argparse
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import statistics

import benchmark

METRICS = ("attempted_calls", "forwarded_calls", "retrieval_bytes", "delivered_bytes",
           "tool_latency_ms", "wall_time_ms")


def distribution(values):
    values = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def eligible(run):
    return bool(run and run.get("status") == "completed" and run.get("repository_unchanged")
                and not run.get("unexpected_tools") and not run.get("client_error")
                and not run.get("mcp_failures"))


def analyze(directory):
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    runs = []
    by_key = {}
    for index, trial in enumerate(plan["trials"]):
        path = directory / f"trial-{index:04d}" / "run.json"
        run = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        record = {"index": index, **trial, "run": run}
        runs.append(record)
        by_key[(trial["task_id"], trial["repetition"], trial["system"])] = run
    systems = []
    for system_id in plan["systems"]:
        selected = [record["run"] for record in runs if record["system"] == system_id]
        present = [run for run in selected if run]
        accepted = [run for run in present if eligible(run)]
        tools = Counter(tool for run in accepted for tool in run.get("tool_sequence", []))
        systems.append({
            "system": system_id,
            "planned": len(selected),
            "recorded": len(present),
            "eligible": len(accepted),
            "completed": sum(run.get("status") == "completed" for run in present),
            "payload_correct": sum(bool(run.get("payload_matches")) for run in accepted),
            "format_correct": sum(bool(run.get("format_correct")) for run in accepted),
            "strict_correct": sum(bool(run.get("correct")) for run in accepted),
            "resolved_correct": sum(bool(run.get("resolved_correct")) for run in accepted),
            "resolved_credit": distribution([run.get("resolved_credit") for run in accepted]),
            "budget_exhausted": sum(bool(run.get("budget_exhausted")) for run in accepted),
            "first_tools": dict(Counter(run.get("tool_sequence", [None])[0]
                                        if run.get("tool_sequence") else None for run in accepted)),
            "tool_counts": dict(tools),
            "metrics": {metric: distribution([run.get(metric) for run in accepted]) for metric in METRICS},
        })
    pairs = []
    task_repetitions = sorted({(trial["task_id"], trial["repetition"]) for trial in plan["trials"]})
    for baseline, variant in itertools.combinations(plan["systems"], 2):
        for task_id, repetition in task_repetitions:
            left = by_key.get((task_id, repetition, baseline))
            right = by_key.get((task_id, repetition, variant))
            accepted = eligible(left) and eligible(right)
            pair = {
                "task_id": task_id, "repetition": repetition,
                "baseline": baseline, "variant": variant, "eligible": accepted,
                "baseline_payload_correct": left.get("payload_matches") if left else None,
                "variant_payload_correct": right.get("payload_matches") if right else None,
                "baseline_resolved_credit": left.get("resolved_credit") if left else None,
                "variant_resolved_credit": right.get("resolved_credit") if right else None,
            }
            for metric in METRICS:
                pair[f"{metric}_difference"] = (
                    right.get(metric) - left.get(metric)
                    if accepted and isinstance(left.get(metric), (int, float))
                    and isinstance(right.get(metric), (int, float)) else None
                )
            pairs.append(pair)
    pair_summaries = []
    for baseline, variant in itertools.combinations(plan["systems"], 2):
        selected = [pair for pair in pairs if pair["baseline"] == baseline and pair["variant"] == variant]
        accepted = [pair for pair in selected if pair["eligible"]]
        pair_summaries.append({
            "baseline": baseline, "variant": variant, "planned": len(selected), "eligible": len(accepted),
            "baseline_payload_correct": sum(bool(pair["baseline_payload_correct"]) for pair in accepted),
            "variant_payload_correct": sum(bool(pair["variant_payload_correct"]) for pair in accepted),
            "baseline_resolved_correct": sum(pair["baseline_resolved_credit"] == 1.0 for pair in accepted),
            "variant_resolved_correct": sum(pair["variant_resolved_credit"] == 1.0 for pair in accepted),
            "resolved_credit_difference": distribution([
                pair["variant_resolved_credit"] - pair["baseline_resolved_credit"] for pair in accepted
                if isinstance(pair["baseline_resolved_credit"], (int, float))
                and isinstance(pair["variant_resolved_credit"], (int, float))]),
            "metrics": {metric: distribution([pair[f"{metric}_difference"] for pair in accepted])
                        for metric in METRICS},
        })
    return {
        "version": "comparison-analysis-v1",
        "manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        "planned_trials": plan["planned_trials"],
        "systems": systems,
        "pair_summaries": pair_summaries,
        "pairs": pairs,
        "runs": runs,
        "manifest": manifest,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(args.input.resolve(strict=True))
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, report)
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
