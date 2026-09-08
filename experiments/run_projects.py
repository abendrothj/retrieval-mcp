#!/usr/bin/env python3
"""Validate the source-checked project suite and run repositories sequentially."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import analyze
import benchmark
from prepare_stratified import CATEGORIES

FORMAT = ('\n\nReturn only one JSON object with exactly the key "answer" containing the requested value. '
          'Do not include Markdown or prose. Qualified symbols use relative/path.rs::Type::method or '
          'relative/path.rs::function; Python uses relative/path.py::Class::method or relative/path.py::function. '
          'For sets return each item once. For call chains retain order. Exclude tests when production code is requested. '
          'Treat anonymous closures as part of their enclosing named function.')


def prepare_questions(snapshots, suite_path, output, pilot):
    suite = json.loads(suite_path.read_text())
    prepared = {}
    for project, tasks in suite["projects"].items():
        manifest = json.loads((snapshots/project/"snapshot.json").read_text())
        root = snapshots/project/"corpus"
        if manifest["revision"] != suite["revisions"][project] or benchmark.fingerprint(root) != manifest["corpus"]:
            raise ValueError(f"snapshot mismatch: {project}")
        if Counter(t["category"] for t in tasks) != dict.fromkeys(CATEGORIES, 2):
            raise ValueError("each repository needs two tasks in every category")
        for task in tasks:
            if not benchmark.grade_answer(task, json.dumps(task["expected_json"]))["correct"]:
                raise ValueError("invalid gold answer")
            for evidence in task["evidence"]:
                if evidence["path"] not in manifest["files"]:
                    raise ValueError("evidence is not in the source allowlist")
                text = (root/evidence["path"]).read_text()
                if not all(anchor in text for anchor in evidence["contains"]):
                    raise ValueError(f"missing gold evidence: {task['id']}")
        chosen = [t for t in tasks if t["category"] == "mixed_discovery_structure"][:1] if pilot else tasks
        prepared[project] = [{**task, "question":task["question"] + FORMAT,
                              "corpus_sha256":manifest["corpus"]["sha256"]} for task in chosen]
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for project, tasks in prepared.items():
        benchmark.write_json(output/f"{project}-questions.json", tasks)
    benchmark.write_json(output/"study.json", {"phase":"pilot" if pilot else "full", "suite":suite,
        "suite_sha256":hashlib.sha256(suite_path.read_bytes()).hexdigest(), "repetitions":1 if pilot else 3,
        "harness_sha256":hashlib.sha256(Path(benchmark.__file__).read_bytes()).hexdigest(),
        "grading":"json-answer-v2",
        "planned_trials":sum(len(t) for t in prepared.values()) * 4 * (1 if pilot else 3),
        "note":"Repositories run sequentially; within-repository order is seeded and shuffled. No concurrent client/model sessions."})
    return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=["pilot","full"], default="pilot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    snapshots, output = args.snapshots.resolve(), args.output.resolve()
    if output.is_relative_to(snapshots):
        parser.error("runs must be outside the snapshot directory")
    project_root = Path(__file__).resolve().parents[1]
    prepared = prepare_questions(snapshots, Path(__file__).with_name("project_questions.json"), output, args.phase == "pilot")
    for project in prepared:
        options = SimpleNamespace(root=snapshots/project/"corpus", output=output/project,
            questions=output/f"{project}-questions.json", server=project_root/"target/release/retrieval-mcp",
            model="claude-sonnet-4-6", client="claude", claude_auth="subscription", agent_command=None,
            semantic_command=[str(project_root/"target/release/examples/ollama_backend")],
            profiles=list("ABCD"), repetitions=1 if args.phase == "pilot" else 3, seed=42, timeout=300,
            tool_timeout=120, max_budget_usd=1, semantic_cache="warm", dry_run=args.dry_run)
        status = benchmark.run(options)
        if not args.dry_run:
            benchmark.write_json(output/f"{project}-analysis.json", analyze.analyze_directory(options.output))
        if status:
            return status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
