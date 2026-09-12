#!/usr/bin/env python3
"""Did the treatment arm stop too early? Classify every question it lost. No model calls.

A stopping rule that cuts calls is only worth having if the calls it cuts were unnecessary. On a
saturated suite that question cannot be asked, because nothing is ever lost. On a discriminating
suite it can, and it has exactly one interesting failure mode:

  premature_stop        - a gold identity never appeared in any tool result in the treatment
                          trial, so the model answered without ever retrieving a required fact.
                          Where the control arm did retrieve it, the expansion the rule skipped
                          was demonstrably available: `expansion_skipped`.
  evidence_seen         - every gold identity was on screen and the answer is still wrong. That
                          is a reasoning or commit failure; the stopping rule did not cause it.

Evidence is judged the way `end_to_end.py` judges first hit: a gold identity counts as retrieved
when its leaf name and its file path both appear in the text a tool returned. That proves
visibility, not comprehension, which is the conservative direction here - it can only make the
rule look better than it is on `evidence_seen`, and it never invents a premature stop.
"""
import argparse
from collections import Counter
import json
from pathlib import Path

import end_to_end
import quality_pass


def tool_text(trial):
    """Everything every tool put into this trial's context, as one string."""
    stream, client = end_to_end.events(trial)
    return "\n".join(record["body"] for record in end_to_end.calls_from(stream, client))


def identities(task):
    """Every (path, leaf) the gold asserts."""
    found = []
    for value in quality_pass.flatten(task["expected_json"]["answer"]):
        path, _, rest = value.partition("::")
        if rest:
            found.append((path, rest.split("::")[-1]))
    return found


def retrieved(text, pairs):
    """The gold identities that were on screen, and those that never were."""
    seen = [pair for pair in pairs if pair[0] in text and pair[1] in text]
    return seen, [pair for pair in pairs if pair not in seen]


def audit(run, questions, corpus, control, treatment):
    tasks = {task["id"]: task for task in json.loads(questions.read_text(encoding="utf-8"))}
    index = quality_pass.definitions(corpus)
    trials = {}
    for path in sorted(run.glob("trial-*/run.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["status"] != "completed":
            continue
        state["_dir"] = path.parent
        trials[(state["task_id"], state["system"])] = state

    def credit(state):
        task = tasks[state["task_id"]]
        return quality_pass.credit(quality_pass.answer_json(state.get("answer")),
                                   task["expected_json"]["answer"], index)

    findings, verdicts = {}, Counter()
    paired = 0
    for task_id in sorted(tasks):
        left, right = trials.get((task_id, control)), trials.get((task_id, treatment))
        if not left or not right:
            continue
        paired += 1
        control_credit, treatment_credit = credit(left), credit(right)
        if treatment_credit >= control_credit:
            continue
        pairs = identities(tasks[task_id])
        seen, missing = retrieved(tool_text(right["_dir"]), pairs)
        control_seen, _ = retrieved(tool_text(left["_dir"]), pairs)
        verdict = "premature_stop" if missing else "evidence_seen"
        expansion_skipped = bool(missing) and any(pair in control_seen for pair in missing)
        verdicts[verdict] += 1
        if expansion_skipped:
            verdicts["expansion_skipped"] += 1
        findings[task_id] = {
            "verdict": verdict,
            "expansion_skipped": expansion_skipped,
            "control_credit": control_credit,
            "treatment_credit": treatment_credit,
            "gold_identities": ["::".join(pair) for pair in pairs],
            "never_retrieved": ["::".join(pair) for pair in missing],
            "control_calls": left["attempted_calls"],
            "treatment_calls": right["attempted_calls"],
            "treatment_answer": (right.get("answer") or "")[-300:],
        }
    regressions = len(findings)
    improvements = sum(
        1 for task_id in tasks
        if (task_id, control) in trials and (task_id, treatment) in trials
        and credit(trials[(task_id, treatment)]) > credit(trials[(task_id, control)]))
    return {
        "version": "closure-audit-v1",
        "run": str(run),
        "control": control,
        "treatment": treatment,
        "questions_paired": paired,
        "treatment_regressions": regressions,
        "treatment_improvements": improvements,
        "verdicts": dict(verdicts),
        "findings": findings,
        "limitations": "A gold identity counts as retrieved when its path and leaf both appear in "
                       "tool output, which proves visibility, not use. Questions where both arms "
                       "score the same are not examined, so this measures the risk the stopping "
                       "rule adds, not the suite's difficulty.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--control", default="retrieval-mcp")
    parser.add_argument("--treatment", default="retrieval-mcp-closure")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.run.resolve(strict=True), args.questions, args.corpus.resolve(strict=True),
                   args.control, args.treatment)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("questions_paired", "treatment_regressions", "treatment_improvements",
                       "verdicts")}, indent=2))
    for task_id, finding in result["findings"].items():
        print(f"\n{task_id}: {finding['verdict']}"
              f"{' (expansion skipped)' if finding['expansion_skipped'] else ''}")
        for identity in finding["never_retrieved"]:
            print(f"  - never retrieved: {identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
