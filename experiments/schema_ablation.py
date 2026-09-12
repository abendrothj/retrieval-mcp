#!/usr/bin/env python3
"""Regrade a tool-surface run and compare each schema tax with observed utility."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

import end_to_end
import quality_pass


def tool_schema_tokens(tools_document):
    """Approximate per-turn schema tokens at four serialized bytes per token."""
    return {
        tool["name"]: len(json.dumps(tool, separators=(",", ":"), ensure_ascii=False).encode()) / 4
        for tool in tools_document["tools"]
    }


def load_rows(run, questions, index):
    rows = []
    for trial in sorted(run.glob("trial-*")):
        record_path = trial / "run.json"
        if not record_path.is_file():
            continue
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("status") != "completed":
            continue
        task = questions[record["task_id"]]
        metrics = end_to_end.trial_metrics(trial, task)
        schema = tool_schema_tokens(json.loads((trial / "tools.json").read_text(encoding="utf-8")))
        calls = int(record.get("forwarded_calls") or metrics["calls"])
        rows.append({
            "task_id": record["task_id"],
            "repetition": record["repetition"],
            "system": record["system"],
            "score": quality_pass.credit(
                quality_pass.answer_json(record.get("answer")),
                task["expected_json"]["answer"],
                index,
            ),
            # Client usage envelopes are not comparable across clients - Claude reports fresh,
            # cached, and replayed prefix separately - so tokens come from the replayed stream,
            # which end_to_end normalises identically for every arm.
            "input_tokens": metrics["tokens"]["input"],
            "calls": calls,
            # Real model turns where the client reports one usage record per inference request;
            # otherwise calls plus the final answer, which is the reproducible lower bound.
            "turns": metrics["steps"] if metrics["steps"] > 1 else calls + 1,
            "context_token_turns": metrics["context_token_turns"],
            "tools": Counter(record.get("tool_sequence") or {}),
            "visible_tools": frozenset(schema),
            "schema_tokens": schema,
        })
    return rows


def stat(values):
    return {
        "total": round(sum(values), 1),
        "median": round(statistics.median(values), 1) if values else None,
        "mean": round(statistics.fmean(values), 1) if values else None,
    }


def analyze(rows, full_system):
    systems = sorted({row["system"] for row in rows})
    full_rows = [row for row in rows if row["system"] == full_system]
    if not full_rows:
        raise ValueError(f"full system {full_system!r} has no completed trials")
    full_by_key = {(row["task_id"], row["repetition"]): row for row in full_rows}
    full_tools = full_rows[0]["visible_tools"]
    system_report = {}
    by_system = {system: [row for row in rows if row["system"] == system] for system in systems}
    for system, current in by_system.items():
        if any(row["visible_tools"] != current[0]["visible_tools"] for row in current):
            raise ValueError(f"{system} changed its visible tool surface within the run")
        pairs = [(full_by_key[(row["task_id"], row["repetition"])], row) for row in current
                 if (row["task_id"], row["repetition"]) in full_by_key]
        regressions = sorted({base["task_id"] for base, arm in pairs
                              if base["score"] == 1.0 and arm["score"] < 1.0})
        improvements = sorted({base["task_id"] for base, arm in pairs
                               if base["score"] < 1.0 and arm["score"] == 1.0})
        system_report[system] = {
            "trials": len(current),
            "correct": sum(row["score"] == 1.0 for row in current),
            "mean_credit": round(statistics.fmean(row["score"] for row in current), 3),
            "visible_tools": sorted(current[0]["visible_tools"]),
            "removed_vs_full": sorted(full_tools - current[0]["visible_tools"]),
            "input_tokens": stat([row["input_tokens"] for row in current]),
            "calls": stat([row["calls"] for row in current]),
            "turns": stat([row["turns"] for row in current]),
            "context_token_turns": stat([row["context_token_turns"] for row in current]),
            "schema_tokens_per_turn": round(sum(current[0]["schema_tokens"].values()), 1),
            "schema_token_turns": round(sum(
                sum(row["schema_tokens"].values()) * row["turns"] for row in current
            )),
            "regressions_vs_full": regressions,
            "improvements_vs_full": improvements,
            "turn_delta_vs_full": sum(arm["turns"] - base["turns"] for base, arm in pairs),
        }

    full_usage = Counter()
    for row in full_rows:
        full_usage.update(row["tools"])
    per_tool = {}
    for tool in sorted(full_tools):
        schema_tokens = full_rows[0]["schema_tokens"][tool]
        matching = [system for system, current in by_system.items()
                    if full_tools - current[0]["visible_tools"] == {tool}]
        ablation = matching[0] if len(matching) == 1 else None
        utility = None
        if ablation:
            arm_by_key = {(row["task_id"], row["repetition"]): row for row in by_system[ablation]}
            pairs = [(base, arm_by_key[key]) for key, base in full_by_key.items() if key in arm_by_key]
            helped = sorted({base["task_id"] for base, arm in pairs
                             if base["score"] == 1.0 and arm["score"] < 1.0})
            utility = {
                "ablation_system": ablation,
                "unique_solves_enabled": len(helped),
                "questions": helped,
                "turns_avoided": sum(arm["turns"] - base["turns"] for base, arm in pairs),
                "input_tokens_avoided": sum(
                    arm["input_tokens"] - base["input_tokens"] for base, arm in pairs
                ),
            }
        per_tool[tool] = {
            "schema_tokens_per_turn": round(schema_tokens, 1),
            "schema_token_turns_paid_by_full": round(
                schema_tokens * sum(row["turns"] for row in full_rows)
            ),
            "calls_in_full": full_usage[tool],
            "leave_one_out_utility": utility,
        }
    return {
        "version": "schema-ablation-v1",
        "full_system": full_system,
        "systems": system_report,
        "per_tool": per_tool,
        "limitations": (
            "Schema tokens use minified JSON bytes / 4 and calls + final answer as a turn proxy. "
            "Unique solves are paired development-set differences, not a population estimate; "
            "tools overlap, so a zero leave-one-out loss can mean redundancy rather than no capability."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--full-system", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    questions = {task["id"]: task for task in json.loads(
        args.questions.resolve(strict=True).read_text(encoding="utf-8")
    )}
    index = quality_pass.definitions(args.corpus.resolve(strict=True))
    rows = load_rows(args.run.resolve(strict=True), questions, index)
    report = analyze(rows, args.full_system)
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({system: {
        "correct": row["correct"],
        "trials": row["trials"],
        "input_tokens": row["input_tokens"]["total"],
        "calls": row["calls"]["total"],
        "schema_token_turns": row["schema_token_turns"],
        "regressions_vs_full": len(row["regressions_vs_full"]),
    } for system, row in report["systems"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
