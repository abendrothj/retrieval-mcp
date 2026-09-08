#!/usr/bin/env python3
"""Summarize one sequential three-repository study without mixing it with its pilot."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean

import analyze
from benchmark import write_json


def report(directory):
    repositories, all_runs, all_comparisons = {}, [], []
    for name in ("modelshare", "pig", "sigil"):
        if not (directory/name).exists():
            continue
        result = analyze.analyze_directory(directory/name)
        repositories[name] = result
        all_runs.extend(result["runs"])
        all_comparisons.extend(result["matched_comparisons"])
    profiles = []
    for repository in [*repositories, "all"]:
        runs = all_runs if repository == "all" else repositories[repository]["runs"]
        for profile in "ABCD":
            planned = [r for r in runs if r["metadata"]["profile"] == profile]
            valid = [r for r in planned if r["eligible_for_comparison"]]
            sessions = [s for r in valid for s in r["sessions"]]
            profiles.append({"repository":repository,"profile":profile,"observed_trials":len(planned),
                "eligible_trials":len(valid),"correct":sum(r["metadata"].get("correct") is True for r in valid),
                "format_correct":sum(r["metadata"].get("format_correct") is True for r in valid),
                "calls":sum(s["calls"] for s in sessions),
                "mean_wall_seconds":mean(r["metadata"]["wall_time_ms"] for r in valid)/1000 if valid else None,
                "retrieval_bytes":sum(s["retrieval_bytes"] for s in sessions),
                "fallbacks":sum(s["fallback_count"] for s in sessions),
                "first_tools":dict(Counter(s["first_tool"] for s in sessions))})
    categories, comparisons = analyze.category_summaries(all_runs, all_comparisons)
    cost, models, contamination = 0, Counter(), []
    for path in sorted(path for name in repositories for path in (directory/name).glob("*/transcript.jsonl")):
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                cost += event.get("total_cost_usd", 0) or 0
            if event.get("type") == "system" and event.get("subtype") == "init":
                models[event.get("model", "unknown")] += 1
                if event.get("skills") or event.get("plugins") or any(not t.startswith("mcp__retrieval__") for t in event.get("tools", [])):
                    contamination.append(str(path.relative_to(directory)))
    return {"profiles":profiles,"category_profiles":categories,"category_comparisons":comparisons,
            "repositories":repositories,"client_reported_cost_usd":cost,"model_sessions":dict(models),
            "initialization_contamination":contamination,
            "limitations":"Single model; small hand-authored task strata, some shared function families. Within-repository paired comparisons are primary. Repositories run sequentially; latency and prompt-cache state vary. Structured JSON correctness does not grade surrounding prose or prove routing causality. Pilot is excluded."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = report(args.directory)
    write_json(args.directory/"analysis.json", result)
    print(json.dumps({key:value for key,value in result.items() if key != "repositories"}, indent=2))


if __name__ == "__main__":
    main()
