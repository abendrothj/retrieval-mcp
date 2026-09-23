#!/usr/bin/env python3
"""Compile LOC-BENCH into this project's suite schema, one corpus per question.

`commit_suite.py` mines questions from upstream commits because every suite in
`experiments/suites/` was authored by an agent session inside this repository. LOC-BENCH answers
the same need better and was curated by people who are not us: 560 localisation instances over
165 repositories, filtered by its authors to those with function-level edits, built to mitigate
contamination, and carrying published baselines - LocAgent reports 77.4% function-level Acc@10
on it, which is the first external calibration point this project has ever had.

What it costs is that a LOC-BENCH instance pins its own `base_commit`, so the corpus is
per question rather than per run. Django alone contributes 35 instances over 33 distinct commits.
This module materialises one tree per instance and records the path on the question; the runner
is what has to learn to read it.

**What is dropped, and why.**

  * `hints_text` never enters a question. Including hints drops SWE-bench Lite's "the issue never
    names the target file" rate from 51.3% to 38.0%, so hints are the documented leak.
  * `patch` and `test_patch` never enter a question: they are the answer.
  * `added_functions` disqualify an instance. A function the patch creates does not exist at
    `base_commit`, so no retrieval surface can find it and none should be scored for failing.
  * An instance whose problem statement spells a gold function, or the file that holds it, is
    dropped, because `validate_suite.py` refuses a leaked identifier. That is 24% of LOC-BENCH by
    the symbol rule alone. The field treats leakage as a covariate to stratify on rather than a
    filter - 51.3% of SWE-bench Lite never names the file, and the rest do - so the sample here
    is deliberately the non-leaking half, and the count dropped is reported rather than buried.

Gold is `edit_functions`, which LOC-BENCH already spells `path:function`; this is one character
from this project's `path::name`. It is checked against `quality_pass.definitions` at the
instance's own commit, because the grader's index is what decides whether a question can be
scored at all.

    locbench_suite.py --rows locbench.json --repos yt-dlp/yt-dlp --corpora /tmp/lb --output s.json

Network: one shallow fetch per instance. No model.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import commit_suite
import comparison_runner
import quality_pass
import validate_suite

# One constant, appended to every problem statement, so no per-question wording decision is made
# in this repository. LOC-BENCH's own text is otherwise untouched.
INSTRUCTION = ("Name the function or method in this repository that must change to address the "
               "issue described above, as path::name.")
INSTRUCTION_MANY = ("Name every function or method in this repository that must change to "
                    "address the issue described above, each as path::name.")


def materialise(repo, commit, into):
    """The repository at one instance's base_commit, fetched shallow.

    A full clone of every instance's repository is not affordable and is not needed: retrieval is
    judged against the tree, never against its history.
    """
    marker = into / ".locbench-commit"
    if marker.is_file() and marker.read_text().strip() == commit:
        return into
    into.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(into), *a], capture_output=True, text=True)
    run("init", "-q")
    run("remote", "add", "origin", f"https://github.com/{repo}")
    fetched = run("fetch", "--depth", "1", "--no-tags", "-q", "origin", commit)
    if fetched.returncode != 0:
        raise RuntimeError(f"{repo}@{commit[:9]}: {fetched.stderr.strip()[:200]}")
    run("checkout", "-q", "--detach", "FETCH_HEAD")
    # The history is dead weight once the tree exists - retrieval is judged against the tree -
    # and it is about half the bytes: 15 sqlglot snapshots cost 1.2 GB with .git and roughly
    # half that without. One corpus per question makes that the binding constraint on how many
    # questions a suite can have.
    shutil.rmtree(into / ".git", ignore_errors=True)
    marker.write_text(commit)
    return into


def deduplicate(corpora):
    """Replace byte-identical files across snapshots with hardlinks.

    One corpus per question is what LOC-BENCH costs, and consecutive snapshots of the same
    repository differ by a handful of files: 15 sqlglot trees are 1.0 GB stored naively and a
    fraction of that stored once. `scale_response.py` already builds its size ladder out of
    hard-link trees for the same reason, and the same precondition holds - every arm reads the
    corpus and none may write to it, which the read-only sandbox already enforces.
    """
    seen, saved, linked = {}, 0, 0
    for path in sorted(corpora.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_nlink > 1:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        key = (digest, stat.st_size)
        first = seen.get(key)
        if first is None:
            seen[key] = path
            continue
        try:
            path.unlink()
            path.hardlink_to(first)
        except OSError:
            continue
        saved += stat.st_size
        linked += 1
    return linked, saved


def gold_for(row, corpus, index=None):
    """(identities, why_refused). Identities are path::name present in the grader's index."""
    if row.get("added_functions"):
        return None, "patch adds a function that does not exist at base_commit"
    raw = row.get("edit_functions") or []
    if not raw:
        return None, "no edit_functions"
    index = quality_pass.definitions(corpus) if index is None else index
    found = []
    for entry in raw:
        path, _, qualified = entry.rpartition(":")
        if not path or not qualified:
            return None, f"unparsable edit_function {entry!r}"
        # LOC-BENCH writes a method as `Class.method`; this project's identity is path-qualified
        # and leaf-named, and `validate_suite.leafwise` treats path::Class::method and
        # path::method as the same definition, so the qualified form is kept and the leaf is
        # what the index is asked about.
        leaf = qualified.split(".")[-1]
        if path not in index.get(leaf, set()):
            return None, f"gold {leaf} is not indexed at {path} in this snapshot"
        identity = f"{path}::" + "::".join(qualified.split("."))
        if identity not in found:
            found.append(identity)
    return found, None


def question_for(row, gold, corpus, corpus_dir, index=None):
    statement = (row.get("problem_statement") or "").strip()
    if commit_suite.leaks(statement, gold, defined_names=index):
        return None, "problem statement names a gold function or its file"
    path = gold[0].split("::")[0]
    anchor = commit_suite.declaration_line(corpus, path, gold[0].split("::")[-1])
    if not anchor:
        return None, "no declaration line to anchor evidence on"
    alternates = commit_suite.alternates(corpus, path, {g.split("::")[-1] for g in gold})
    if not alternates:
        return None, "no other definition in the file to offer as a wrong answer"
    instruction = INSTRUCTION if len(gold) == 1 else INSTRUCTION_MANY
    return {
        "id": row["instance_id"],
        "category": row.get("category") or "Bug Report",
        "set": "holdout",
        "question": f"{statement}\n\n{instruction}",
        "expected_json": {"answer": gold[0] if len(gold) == 1 else gold},
        "rejected_alternates": alternates,
        "evidence": [{"path": path, "contains": anchor}],
        "author_notes":
            f"LOC-BENCH V1 instance {row['instance_id']} ({row['repo']} @ "
            f"{row['base_commit'][:9]}), curated upstream and not authored in this repository. "
            f"The prose is the issue's own problem statement with one constant sentence "
            f"appended; hints_text, patch and test_patch are excluded. Gold is the instance's "
            f"edit_functions, verified against quality_pass.definitions at this commit.",
        # The corpus is per question here, which no earlier suite in this project needed.
        "corpus": str(corpus_dir),
        "covariates": {
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "gold_cardinality": len(gold),
            "locbench_category": row.get("category"),
            "names_target": commit_suite.names_target(statement, gold),
            "statement_chars": len(statement),
        },
    }, None


def build(rows, repos, corpora, limit, single_only):
    questions, refused = [], Counter()
    for row in rows:
        if limit and len(questions) >= limit:
            break
        if repos and row["repo"] not in repos:
            continue
        if single_only and len(row.get("edit_functions") or []) != 1:
            refused["multi-function gold (excluded by --single-only)"] += 1
            continue
        target = corpora / row["instance_id"]
        try:
            corpus = materialise(row["repo"], row["base_commit"], target)
        except RuntimeError as failure:
            refused[f"fetch failed"] += 1
            print(f"   skip {row['instance_id']}: {failure}")
            continue
        index = quality_pass.definitions(corpus)
        gold, why = gold_for(row, corpus, index)
        if gold is None:
            refused[why.split(" in this snapshot")[0] if why else "no gold"] += 1
            continue
        entry, why = question_for(row, gold, corpus, target, index)
        if entry is None:
            refused[why] += 1
            continue
        entry["covariates"]["corpus_fingerprint"] = comparison_runner.source_fingerprint(corpus)
        questions.append(entry)
    return questions, refused


def compile_each(questions):
    """Compile every question against its own corpus, since validate_suite takes one at a time."""
    problems = {}
    with tempfile.TemporaryDirectory() as raw:
        for task in questions:
            path = Path(raw) / f"{task['id']}.json"
            path.write_text(json.dumps([task]), encoding="utf-8")
            result = validate_suite.validate(path, Path(task["corpus"]))
            if result["problems"]:
                problems[task["id"]] = result["findings"]
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=Path, required=True, help="LOC-BENCH metadata as JSON")
    parser.add_argument("--repos", nargs="*", default=[], help="restrict to these repositories")
    parser.add_argument("--corpora", type=Path, required=True, help="where trees are materialised")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--single-only", action="store_true",
                        help="single-function golds only; an exhaustive set at k=1 is below the "
                             "operating point the field reports (LocAgent: 77.4%% Acc@10)")
    parser.add_argument("--dedupe", action="store_true",
                        help="hardlink byte-identical files across snapshots; arms read the "
                             "corpus and never write to it")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    rows = json.loads(args.rows.read_text(encoding="utf-8"))
    questions, refused = build(rows, set(args.repos), args.corpora.resolve(), args.limit,
                               args.single_only)
    if args.dedupe:
        linked, saved = deduplicate(args.corpora.resolve())
        print(f"deduplicated {linked} files, {saved/1e9:.2f} GB reclaimed")
    problems = compile_each(questions)
    report = {
        "version": "locbench-suite-v1",
        "source": "LOC-BENCH V1 (czlll/Loc-Bench_V1)",
        "considered": len(rows) if not args.repos else
                      sum(1 for r in rows if r["repo"] in set(args.repos)),
        "questions": len(questions),
        "refused": dict(refused),
        "questions_failing_validate_suite": problems,
        "covariates": {
            "by_category": dict(Counter(q["category"] for q in questions)),
            "by_repo": dict(Counter(q["covariates"]["repo"] for q in questions)),
            "names_target": sum(1 for q in questions if q["covariates"]["names_target"]),
        },
    }
    if args.output:
        args.output.write_text(json.dumps(questions, indent=1) + "\n", encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items()
                      if k != "questions_failing_validate_suite"}, indent=1))
    print("failing validate_suite:", len(problems))
    return 1 if problems or not questions else 0


if __name__ == "__main__":
    raise SystemExit(main())
