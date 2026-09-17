#!/usr/bin/env python3
"""Prepare a hash-pinned source snapshot and separate answer key; never starts a model."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from benchmark import fingerprint, grade_answer, write_json

CATEGORIES = {"exact_lookup", "conceptual_lookup", "symbol_resolution", "direct_caller_lookup",
              "transitive_blast_radius", "mixed_discovery_structure"}
ANSWER_FORMAT = (
    '\n\nReturn only a JSON object with exactly one key, "answer", containing the requested value. '
    'Do not add Markdown fences or explanatory prose. A qualified symbol means '
    'repository-relative/file.rs::Type::method, or repository-relative/file.rs::function '
    'for a free function. Use the implementing type for trait methods. '
    'For set answers, return each item once; for ordered chains, preserve call order. '
    'Treat closures as part of their enclosing project function. '
    'Exclude #[cfg(test)] modules whenever production code is requested.'
)


def prepare(source, output, suite_path):
    source, output = source.resolve(strict=True), output.resolve()
    if not source.is_dir() or output.is_relative_to(source):
        raise ValueError("output must be a new directory outside the source repository")
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    if suite.get("schema_version") != 1:
        raise ValueError("unsupported suite version")
    counts = Counter(task["category"] for task in suite["tasks"])
    if set(counts) != CATEGORIES or len(set(counts.values())) != 1 or min(counts.values()) < 4:
        raise ValueError("suite must have at least four tasks in each of six balanced categories")
    if len({task["id"] for task in suite["tasks"]}) != len(suite["tasks"]):
        raise ValueError("suite IDs must be unique")
    # Read exact allowlisted bytes before writing anything. No symlinks, tree walk, or answer files.
    corpus = {}
    for relative, expected_hash in suite["corpus_files"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("invalid corpus path")
        candidate = source
        for component in path.parts:
            candidate = candidate / component
            if candidate.is_symlink():
                raise ValueError("corpus paths must not be symlinks")
        if not candidate.is_file() or candidate.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("invalid or oversized corpus file")
        data = candidate.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError(f"corpus changed: {relative}; review the answer key before repinning")
        corpus[relative] = data
    for task in suite["tasks"]:
        if not grade_answer(task, json.dumps(task["expected_json"]))["correct"]:
            raise ValueError(f"invalid gold answer: {task['id']}")
        if not task.get("evidence"):
            raise ValueError("every task needs source evidence")
        for evidence in task["evidence"]:
            text = corpus[evidence["path"]].decode("utf-8")
            if not evidence["contains"] or not all(anchor in text for anchor in evidence["contains"]):
                raise ValueError(f"gold evidence missing: {task['id']}")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    root = output / "corpus"
    root.mkdir(mode=0o700)
    for relative, data in corpus.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    identity = fingerprint(root)
    tasks = [{**task, "question": task["question"] + ANSWER_FORMAT, "corpus_sha256": identity["sha256"]}
             for task in suite["tasks"]]
    write_json(output / "questions.json", tasks)
    manifest = {"suite": suite["name"], "suite_sha256": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
                "corpus": identity, "corpus_files": suite["corpus_files"], "categories": dict(counts),
                "source_files": sum(path.endswith(".rs") for path in corpus),
                "source_lines": sum(len(data.splitlines()) for path, data in corpus.items() if path.endswith(".rs")),
                "planned_trials_at_three_repetitions": len(tasks) * 4 * 3}
    write_json(output / "suite-manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.source, args.output, Path(__file__).resolve().parent / "suites/stratified.json")
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"prepare: {error}\n")


if __name__ == "__main__":
    main()
