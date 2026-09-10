#!/usr/bin/env python3
"""Compile a question suite: prove its gold and its grader before any model runs.

Five graders in this project have produced or nearly produced false findings, and an audit of one
frozen suite found two golds that were simply wrong about the corpus. A suite is therefore not
usable until it passes these checks, which need no model and no network:

  Gold truth      - every gold identity is a real definition at the path it claims; every claimed
                    caller really calls the helper, by an independent ripgrep enumeration; a set
                    declared exhaustive contains every caller found that way.
  Answerability   - a gold whose name has namesakes is path-qualified, or the question lists the
                    other identities as acceptable; a plausible alternate answer is either accepted
                    or recorded as explicitly rejected.
  Grader behaviour- synthetic answers score what they must: the canonical gold scores 1, prose
                    naming every identity scores 1, a partial answer scores its share, a known
                    wrong answer scores 0, and a bare ambiguous namesake scores 0.

Exit status is nonzero when any check fails, so this runs as a build step.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

import audit_failures
import quality_pass

WRONG = "src/definitely/not/a/real/path.ts::definitelyNotARealSymbol"


def qualified(identity):
    return isinstance(identity, str) and "::" in identity


def prose(identities):
    """The same answer a model would write as a sentence."""
    names = [identity.split("::")[-1] for identity in identities]
    paths = sorted({identity.split("::")[0] for identity in identities})
    return f"The answer is {', '.join(names)} (in {', '.join(paths)})."


def check_question(task, index, corpus):
    """Every failed expectation for one question, as (severity, detail) pairs."""
    gold = task["expected_json"]["answer"]
    problems = []
    report = lambda severity, detail: problems.append({"severity": severity, "detail": detail})
    identities = quality_pass.flatten(gold)
    if gold is None:
        # A null-answer question asserts absence; it needs no identities but must still grade.
        if quality_pass.credit(None, None, index) != 1.0:
            report("grader", "null gold does not credit an abstention")
        if quality_pass.credit("something", None, index) != 0.0:
            report("grader", "null gold credits a claim")
        return problems

    for identity in identities:
        if not qualified(identity):
            report("gold", f"gold identity is not path-qualified: {identity!r}")
            continue
        path, leaf = identity.split("::", 1)
        leaf = leaf.split("::")[-1]
        defined = index.get(leaf, set())
        if not defined:
            report("gold", f"gold names {leaf}, which no file in the corpus defines")
        elif path not in defined:
            report("gold", f"gold places {leaf} in {path}; the corpus defines it in "
                           f"{sorted(defined)}")
        # Answerability: with namesakes present, only a path-qualified answer can be credited, so
        # the question must ask for one or accept the other identities.
        if len(defined) > 1 and not any(
                other.split("::")[-1] == leaf for other in (task.get("acceptable_symbols") or [])):
            if "::" not in task["question"] and "qualified" not in task["question"]:
                report("answerability",
                       f"{leaf} is defined {len(defined)} times and the question does not ask for "
                       f"a qualified symbol, so a correct bare answer cannot score")

    def leafwise(identity):
        """path::Class::method and path::method name the same definition to a syntactic index."""
        path, _, rest = identity.partition("::")
        return f"{path}::{rest.split('::')[-1]}"

    helper = task.get("helper")
    if helper and qualified(helper):
        path, name = helper.split("::", 1)
        verified = {leafwise(entry)
                    for entry in audit_failures.true_callers(corpus, name.split("::")[-1], path)}
        claimed = {leafwise(identity) for identity in identities if identity != helper}
        missing = claimed - verified
        if missing:
            report("gold", f"gold claims callers of {name} that the corpus does not show: "
                           f"{sorted(missing)}")
        if task.get("exhaustive") and verified - claimed:
            report("gold", f"gold is declared exhaustive but omits {len(verified - claimed)} "
                           f"callers of {name}: {sorted(verified - claimed)[:5]}")
    elif task["category"] in ("direct_caller_lookup", "mixed_discovery_structure") and not helper:
        report("answerability", "a caller question must name the helper it is about in `helper`, "
                                "so its gold can be verified against the corpus")

    # Grader behaviour, against synthetic answers only.
    if quality_pass.credit(gold, gold, index) != 1.0:
        report("grader", "the canonical gold answer does not score 1")
    if identities and quality_pass.credit(prose(identities), gold, index) != 1.0:
        report("grader", "prose naming every gold identity does not score 1")
    if quality_pass.credit(WRONG, gold, index) != 0.0:
        report("grader", "a wrong symbol scores above 0")
    if len(identities) > 1:
        part = quality_pass.credit(prose(identities[:1]), gold, index)
        share = 1 / len(identities)
        if abs(part - share) > 1e-9:
            report("grader", f"naming 1 of {len(identities)} identities scores {part:.3f}, "
                             f"expected {share:.3f}")
    for identity in identities:
        leaf = identity.split("::")[-1]
        if len(index.get(leaf, ())) > 1 and quality_pass.credit(leaf, gold, index) > 0:
            report("grader", f"a bare ambiguous name ({leaf}) scores above 0")
    for alternate in (task.get("rejected_alternates") or []):
        if quality_pass.credit(alternate, gold, index) > 0:
            report("grader", f"explicitly rejected alternate {alternate} scores above 0")
    return problems


def validate(questions_path, corpus):
    tasks = json.loads(questions_path.read_text(encoding="utf-8"))
    index = quality_pass.definitions(corpus)
    report, failed = {}, 0
    for task in tasks:
        problems = check_question(task, index, corpus)
        report[task["id"]] = problems
        failed += bool(problems)
    return {
        "version": "validate-suite-v1",
        "questions": len(tasks),
        "questions_with_problems": failed,
        "problems": sum(len(value) for value in report.values()),
        "by_severity": dict(Counter(problem["severity"]
                                    for value in report.values() for problem in value)),
        "by_category": dict(Counter(task["category"] for task in tasks)),
        "findings": {key: value for key, value in report.items() if value},
        "limitations": "Checks the gold against the corpus and the grader against synthetic "
                       "answers. It cannot tell whether a question is interesting, and it reads "
                       "callers syntactically, exactly as the systems under test do.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.questions.resolve(strict=True), args.corpus.resolve(strict=True))
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("questions", "questions_with_problems", "problems", "by_severity")},
                     indent=2))
    for task_id, problems in result["findings"].items():
        print(f"\n{task_id}")
        for problem in problems:
            print(f"  - [{problem['severity']}] {problem['detail']}")
    return 1 if result["questions_with_problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
