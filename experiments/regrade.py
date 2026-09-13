#!/usr/bin/env python3
"""Re-score archived trials with the current grader, in place, without calling a model.

A repaired grader is only useful if it is applied to every arm at once: the ledger's defects were
found because a fix was re-run symmetrically and the whole table moved together. This recomputes
`resolved_credit`/`resolved_correct` for every trial of a comparison run from the answer text the
trial already stored, using exactly the call `comparison_runner` makes, and records what changed.

It never invents a score for a trial that has no answer, and it writes the previous value beside
the new one so a re-graded run can always be read back to its frozen numbers.
"""
import argparse
import json
from pathlib import Path

import benchmark
import quality_pass


def regrade(run, corpus, questions_path):
    tasks = {task["id"]: task for task in json.loads(questions_path.read_text(encoding="utf-8"))}
    index = quality_pass.definitions(corpus)
    changes = []
    for trial in sorted(run.glob("trial-*")):
        state_path = trial / "run.json"
        if not state_path.is_file():
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        task = tasks.get(state.get("task_id"))
        if task is None or not isinstance(state.get("answer"), str):
            continue
        credit = quality_pass.credit(
            quality_pass.answer_json(state["answer"]), task["expected_json"]["answer"], index)
        previous = state.get("resolved_credit")
        if previous is not None and abs(previous - credit) < 1e-9:
            continue
        changes.append({"trial": trial.name, "task_id": state["task_id"],
                        "system": state.get("system"), "from": previous, "to": credit})
        state["resolved_credit_before_regrade"] = previous
        state["resolved_credit"] = credit
        state["resolved_correct"] = credit == 1.0
        benchmark.write_json(state_path, state)
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--reason", required=True, help="why the grader changed")
    args = parser.parse_args()
    run = args.run.resolve(strict=True)
    changes = regrade(run, args.corpus.resolve(strict=True), args.questions.resolve(strict=True))
    record = {"version": "regrade-v1", "reason": args.reason,
              "grader": "quality-pass-v1 symbol resolution and graded credit",
              "changed": changes}
    path = run / "regrade.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing = existing if isinstance(existing, list) else [existing]
    benchmark.write_json(path, existing + [record])
    print(json.dumps({"run": str(run), "changed": len(changes)}, indent=2))
    for change in changes:
        print(f"  {change['system']:16} {change['task_id']:42} "
              f"{change['from']} -> {change['to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
