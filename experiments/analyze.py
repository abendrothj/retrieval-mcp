#!/usr/bin/env python3
"""Analyze version-1 invocation logs and matched benchmark trials (stdlib only)."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import posixpath
from statistics import mean, median

LOW_LEVEL = {"search_exact", "read_source"}
ADVANCED = {"find_symbol", "find_callers", "search_semantic"}


def method(tool):
    return "structural" if tool in {"find_symbol", "find_callers"} else tool


def normalized_arguments(tool, arguments):
    args = {key: value for key, value in arguments.items() if value is not None}
    defaults = {
        "search_exact": {"path": ".", "regex": False, "case_sensitive": True, "limit": 20, "offset": 0},
        "find_symbol": {"path": ".", "limit": 20, "offset": 0},
        "find_callers": {"path": ".", "include_references": False, "limit": 20, "offset": 0},
        "search_semantic": {"limit": 20, "offset": 0},
        "read_source": {"start_line": 1},
    }
    merged = {**defaults.get(tool, {}), **args}
    if tool == "read_source":
        start = merged["start_line"]
        if isinstance(start, int):
            merged.setdefault("end_line", start + 99)
    if isinstance(merged.get("path"), str):
        merged["path"] = posixpath.normpath(merged["path"])
    return tool, json.dumps(merged, sort_keys=True, ensure_ascii=False)


def intervals(event):
    for location in event.get("locations", []):
        path, start = location.get("path"), location.get("line")
        end = location.get("end_line") or start
        if isinstance(path, str) and isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
            yield path, start, end


def overlap(a, b):
    return a[0] == b[0] and max(a[1], b[1]) <= min(a[2], b[2])


def covered(interval, previous):
    path, start, end = interval
    position = start
    for _, left, right in sorted((i for i in previous if i[0] == path), key=lambda i: i[1]):
        if left > position:
            break
        position = max(position, right + 1)
        if position > end:
            return True
    return False


def analyze_events(events):
    sessions, warnings = defaultdict(dict), []
    for index, event in enumerate(events):
        if event.get("event") not in {"tool_start", "tool_end"}:
            continue
        if event.get("schema_version") != 1:
            raise ValueError("unsupported invocation schema_version")
        if not isinstance(event.get("session_id"), str) or not isinstance(event.get("sequence"), int):
            raise ValueError("invocation event requires session_id and integer sequence")
        call = sessions[event["session_id"]].setdefault(event["sequence"], {})
        kind = "start" if event["event"] == "tool_start" else "end"
        if kind in call:
            warnings.append(f"duplicate {kind}: {event['session_id']}/{event['sequence']}")
            continue
        call[kind], call[kind + "_order"] = event, index
    summaries = []
    for session_id, calls in sessions.items():
        ordered = [(sequence, call) for sequence, call in sorted(calls.items()) if "start" in call]
        warnings.extend(f"orphan completion: {session_id}/{seq}" for seq, call in calls.items() if "start" not in call)
        sequence = [call["start"]["tool"] for _, call in ordered]
        counts = Counter(sequence)
        results = {"session_id": session_id, "first_tool": sequence[0] if sequence else None,
                   "tool_sequence": sequence, "tool_counts": dict(counts), "calls": len(ordered),
                   "completed_calls": sum("end" in call for _, call in ordered),
                   "incomplete_calls": sum("end" not in call for _, call in ordered),
                   "errors": sum(call.get("end", {}).get("error") is not None for _, call in ordered),
                   "tool_switches": sum(a != b for a,b in zip(sequence, sequence[1:])),
                   "method_switches": sum(method(a) != method(b) for a,b in zip(sequence, sequence[1:])),
                   "fallback_proxies": [], "duplicate_successful_requests": [], "redundant_read_proxies": [],
                   "repeated_location_calls": [], "advanced_followups": [], "semantic_to_structural": []}
        results["retrieval_bytes"] = sum(call.get("end", {}).get("retrieval_bytes", 0) for _,call in ordered)
        results["response_bytes"] = sum(call.get("end", {}).get("response_bytes", 0) for _,call in ordered)
        results["summed_latency_ms"] = sum(call.get("end", {}).get("latency_ms", 0) for _,call in ordered)
        for position, (seq, call) in enumerate(ordered):
            start, end = call["start"], call.get("end")
            # File order provides a strict happens-before test, including same-millisecond events.
            prior = [(s,c) for s,c in ordered[:position] if "end" in c and c["end_order"] < call["start_order"]]
            successful = [(s,c) for s,c in prior if c["end"].get("error") is None]
            if prior:
                prior_seq, previous = max(prior, key=lambda entry: entry[1]["end_order"])
                failure = previous["end"].get("error") is not None
                empty = previous["end"].get("result_count") == 0
                if method(previous["start"]["tool"]) != method(start["tool"]) and (failure or empty):
                    results["fallback_proxies"].append({"from_sequence": prior_seq, "to_sequence": seq,
                                                         "reason": "error" if failure else "empty_result"})
            if not end or end.get("error") is not None:
                continue
            signature = normalized_arguments(start["tool"], start.get("arguments", {}))
            duplicates = [s for s,c in successful if normalized_arguments(c["start"]["tool"], c["start"].get("arguments", {})) == signature]
            if duplicates:
                results["duplicate_successful_requests"].append({"sequence": seq, "prior_sequence": duplicates[-1]})
            locations = list(intervals(end))
            old_locations = [i for _,c in successful for i in intervals(c["end"])]
            if locations and all(covered(i, old_locations) for i in locations):
                results["repeated_location_calls"].append(seq)
            if start["tool"] == "read_source":
                old_reads = [i for _,c in successful if c["start"]["tool"] == "read_source" for i in intervals(c["end"])]
                if locations and all(covered(i, old_reads) for i in locations):
                    results["redundant_read_proxies"].append(seq)
            if start["tool"] in ADVANCED and end.get("result_count", 0) > 0:
                later = [(s,c) for s,c in ordered[position + 1:] if c["start_order"] > call["end_order"]]
                low_counts = Counter(c["start"]["tool"] for _,c in later if c["start"]["tool"] in LOW_LEVEL)
                verified = [s for s,c in later if c["start"]["tool"] == "read_source" and "end" in c
                            and c["end"].get("error") is None and any(overlap(a,b) for a in locations for b in intervals(c["end"]))]
                if start["tool"] == "search_semantic":
                    for s, c in later:
                        if c["start"]["tool"] in {"find_symbol", "find_callers"}:
                            results["semantic_to_structural"].append({"semantic_sequence": seq, "structural_sequence": s,
                                "structural_tool": c["start"]["tool"],
                                "overlapping_evidence": c.get("end", {}).get("error") is None and
                                    any(overlap(a, b) for a in locations for b in intervals(c.get("end", {})))})
                results["advanced_followups"].append({"sequence": seq, "tool": start["tool"],
                    "subsequent_search_exact": low_counts["search_exact"], "subsequent_read_source": low_counts["read_source"],
                    "overlapping_source_verifications": verified,
                    "structural_coverage_uncertain": end.get("coverage", {}).get("complete") is False if isinstance(end.get("coverage"), dict) else None})
        results["fallback_count"] = len(results["fallback_proxies"])
        results["duplicate_request_count"] = len(results["duplicate_successful_requests"])
        results["redundant_read_count"] = len(results["redundant_read_proxies"])
        summaries.append(results)
    return {"sessions": summaries, "warnings": warnings}


def read_events(path):
    events, warnings = [], []
    if not path.exists():
        return [], ["server log missing; zero calls cannot be distinguished from missing telemetry"]
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("expected event object")
                events.append(event)
            except ValueError:
                warnings.append(f"invalid/truncated JSON at line {number}")
    return events, warnings


def category_summaries(runs, comparisons):
    """Descriptive strata only: repeated trials are not independent new questions."""
    groups = defaultdict(list)
    for run in runs:
        groups[(run["metadata"].get("category", "uncategorized"), run["metadata"]["profile"])].append(run)
    summaries = []
    for (category, profile), group in sorted(groups.items()):
        eligible = [r for r in group if r["eligible_for_comparison"]]
        sessions = [s for r in eligible for s in r["sessions"]]
        scored = [r for r in eligible if isinstance(r["metadata"].get("correct"), bool)]
        formatted = [r for r in eligible if isinstance(r["metadata"].get("format_correct"), bool)]
        def distribution(values):
            return {"n": len(values), "mean": mean(values) if values else None,
                    "median": median(values) if values else None}
        summaries.append({"category": category, "profile": profile,
            "questions": len({r["metadata"]["task_id"] for r in group}), "planned_trials": len(group),
            "eligible_trials": len(eligible), "excluded_trials": len(group) - len(eligible),
            "scored_trials": len(scored), "correct_trials": sum(r["metadata"]["correct"] for r in scored),
            "accuracy": mean(r["metadata"]["correct"] for r in scored) if scored else None,
            "format_scored_trials": len(formatted), "format_correct_trials": sum(r["metadata"]["format_correct"] for r in formatted),
            "first_tools_by_session": dict(Counter(s["first_tool"] for s in sessions)),
            "tool_counts": dict(sum((Counter(s["tool_counts"]) for s in sessions), Counter())),
            "calls_per_trial": distribution([sum(s["calls"] for s in r["sessions"]) for r in eligible]),
            "wall_time_ms": distribution([r["metadata"]["wall_time_ms"] for r in eligible if "wall_time_ms" in r["metadata"]]),
            "retrieval_bytes_per_trial": distribution([sum(s["retrieval_bytes"] for s in r["sessions"]) for r in eligible]),
            "input_tokens_per_trial": {field: distribution([r["metadata"]["usage"][field] for r in eligible
                if isinstance(r["metadata"].get("usage"), dict) and isinstance(r["metadata"]["usage"].get(field), (int, float))])
                for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")},
            "fallbacks": sum(s["fallback_count"] for s in sessions),
            "duplicate_requests": sum(s["duplicate_request_count"] for s in sessions),
            "redundant_reads": sum(s["redundant_read_count"] for s in sessions),
            "sessions_with_semantic_then_structural": sum(bool(s["semantic_to_structural"]) for s in sessions),
            "uncertain_structural_responses": sum(a["structural_coverage_uncertain"] is True for s in sessions for a in s["advanced_followups"]),
            "uncertain_structural_responses_verified": sum(a["structural_coverage_uncertain"] is True and bool(a["overlapping_source_verifications"])
                for s in sessions for a in s["advanced_followups"])})
    pairs = defaultdict(list)
    for comparison in comparisons:
        pairs[(comparison["category"], comparison["baseline"], comparison["variant"])].append(comparison)
    matched = []
    for (category, baseline, variant), group in sorted(pairs.items()):
        eligible = [c for c in group if c["eligible"]]
        correct = [c for c in eligible if c["both_correct"]]
        matched.append({"category": category, "baseline": baseline, "variant": variant,
            "pairs": len(group), "eligible_pairs": len(eligible), "both_correct_pairs": len(correct),
            "mean_saved": {field: mean(c[field] for c in eligible) if eligible else None
                           for field in ("search_exact_saved", "read_source_saved", "total_calls_saved", "wall_time_ms_saved")},
            "both_correct_mean_saved": {field: mean(c[field] for c in correct) if correct else None
                                        for field in ("search_exact_saved", "read_source_saved", "total_calls_saved", "wall_time_ms_saved")}})
    return summaries, matched


def analyze_directory(directory):
    runs = []
    for path in sorted(directory.glob("*/run.json")):
        metadata = json.loads(path.read_text())
        events, warnings = read_events(path.parent / "server.jsonl")
        metrics = analyze_events(events)
        metrics["warnings"].extend(warnings)
        metrics["metadata"] = metadata
        metrics["tool_counts"] = dict(sum((Counter(s["tool_counts"]) for s in metrics["sessions"]), Counter()))
        metrics["eligible_for_comparison"] = (metadata.get("status") == "completed" and metadata.get("repository_unchanged") is True
            and not metadata.get("unexpected_tools") and not metrics["warnings"] and all(s["incomplete_calls"] == 0 for s in metrics["sessions"]))
        runs.append(metrics)
    groups = defaultdict(dict)
    for run in runs:
        m = run["metadata"]
        key = (m["task_id"], m["trial"], m["model"], m["client"], m["repository"]["sha256"], m["prompt_sha256"], m.get("semantic_cache"), m.get("server_sha256"), m.get("category", "uncategorized"), m.get("grading", "exact-text-v1"))
        if m["profile"] in groups[key]:
            raise ValueError("duplicate matched trial/profile")
        groups[key][m["profile"]] = run
    comparisons = []
    for key, profiles in groups.items():
        for baseline, variant in [("A","B"), ("A","C"), ("A","D"), ("B","D"), ("C","D")]:
            if baseline not in profiles or variant not in profiles:
                continue
            before, after = profiles[baseline], profiles[variant]
            eligible = before["eligible_for_comparison"] and after["eligible_for_comparison"]
            comparisons.append({"task_id": key[0], "trial": key[1], "baseline": baseline, "variant": variant,
                "category": key[8],
                "eligible": eligible, "both_correct": before["metadata"].get("correct") is True and after["metadata"].get("correct") is True,
                "baseline_correct": before["metadata"].get("correct"), "variant_correct": after["metadata"].get("correct"),
                "search_exact_saved": before["tool_counts"].get("search_exact",0) - after["tool_counts"].get("search_exact",0) if eligible else None,
                "read_source_saved": before["tool_counts"].get("read_source",0) - after["tool_counts"].get("read_source",0) if eligible else None,
                "total_calls_saved": sum(before["tool_counts"].values()) - sum(after["tool_counts"].values()) if eligible else None,
                "wall_time_ms_saved": before["metadata"].get("wall_time_ms", 0) - after["metadata"].get("wall_time_ms", 0) if eligible else None})
    strata, matched_strata = category_summaries(runs, comparisons)
    return {"schema_version": 1, "runs": runs, "matched_comparisons": comparisons,
            "category_profiles": strata, "category_comparisons": matched_strata,
            "interpretation": "Positive saved counts are fewer total low-level calls in a matched trial, not a causal estimate. Advanced follow-ups count all later calls after each successful advanced response and can overlap. Fallback/redundancy/verification are observable proxies; inspect transcripts for intent and correctness."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="benchmark output directory or one server JSONL file")
    args = parser.parse_args()
    try:
        if args.input.is_dir():
            result = analyze_directory(args.input)
        else:
            events, warnings = read_events(args.input)
            result = analyze_events(events)
            result["warnings"].extend(warnings)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"analysis: {error}\n")


if __name__ == "__main__":
    main()
