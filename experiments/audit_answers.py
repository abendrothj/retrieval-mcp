#!/usr/bin/env python3
"""Offline, post-hoc JSON payload audit. Never changes frozen scores or calls a model."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from benchmark import grade_answer


def audit_answer(task, answer):
    # Conservative ceiling: braces in prose or multiple objects require manual review.
    # This audits a payload, not whether surrounding prose contradicts it.
    if not isinstance(answer, str) or answer.count("```") not in (0, 2):
        return "manual_review"
    start = answer.find("{")
    if start < 0:
        return "manual_review"
    try:
        _, length = json.JSONDecoder().raw_decode(answer[start:])
    except ValueError:
        return "manual_review"
    if "{" in answer[start + length:]:
        return "manual_review"
    payload = answer[start:start + length]
    # Reuse the frozen typed/set comparison, including duplicate-key rejection.
    result = grade_answer(task, payload)
    if not result["format_correct"]:
        return "manual_review"
    return "payload_matches" if result["correct"] else "payload_differs"


def report(directory):
    rows, counts = [], {}
    for project in ("modelshare", "pig", "sigil"):
        question_path = directory/f"{project}-questions.json"
        if not question_path.exists():
            continue
        questions = {q["id"]:q for q in json.loads(question_path.read_text())}
        question_hash = hashlib.sha256(question_path.read_bytes()).hexdigest()
        for path in sorted((directory/project).glob("*/run.json")):
            raw = path.read_bytes()
            run = json.loads(raw)
            outcome = (audit_answer(questions[run["task_id"]], run.get("answer"))
                       if run["status"] == "completed" else "excluded_unfinished")
            key = f"{project}/{run['profile']}"
            count = counts.setdefault(key, Counter())
            count[outcome] += 1
            if run["status"] == "completed":
                count["completed"] += 1
                count["original_passes"] += run.get("correct") is True
                count["recovered_payload_matches"] += outcome == "payload_matches" and run.get("correct") is False
            rows.append({"run":str(path.relative_to(directory)), "run_sha256":hashlib.sha256(raw).hexdigest(),
                         "questions_sha256":question_hash, "original_correct":run.get("correct"),
                         "original_format_correct":run.get("format_correct"), "audit":outcome})
    return {"audit_version":"posthoc-single-json-v1", "counts":counts, "runs":rows,
            "limitations":"Post-hoc sensitivity check, not a replacement grader or human review. Only one JSON object is compared with frozen gold; prose contradictions are not evaluated. No model calls or original-file writes."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New audit file; existing files are never overwritten")
    args = parser.parse_args()
    result = report(args.directory)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result["counts"], indent=2))


if __name__ == "__main__":
    main()
