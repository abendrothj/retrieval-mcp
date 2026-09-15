#!/usr/bin/env python3
"""Compile a question suite: prove its gold and its grader before any model runs.

Five graders in this project have produced or nearly produced false findings, and an audit of one
frozen suite found two golds that were simply wrong about the corpus. A suite is therefore not
usable until it passes these checks, which need no model and no network:

  Schema and anchors- required fields have the right shape; question IDs are unique; each exact
                    evidence snippet exists at its corpus-relative source path.
  Gold truth       - every gold identity and helper is a real definition at the path it claims;
                    every claimed caller really calls the helper, by an independent ripgrep
                    enumeration; a set declared exhaustive contains every caller found that way.
  Answerability    - questions do not leak target identifiers; namesake answers explicitly request
                    a qualified symbol; plausible wrong answers are explicitly rejected.
  Grader behaviour - synthetic answers score what they must: the canonical gold scores 1, prose
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
REQUIRED_FIELDS = {
    "id", "category", "set", "question", "expected_json",
    "rejected_alternates", "evidence", "author_notes",
}


def check_structure(task, corpus):
    """Schema and source-anchor failures that must be fixed before semantic checks."""
    problems = []
    report = lambda detail: problems.append({"severity": "schema", "detail": detail})
    missing = sorted(REQUIRED_FIELDS - task.keys())
    if missing:
        report(f"missing required fields: {missing}")
    if not isinstance(task.get("id"), str) or not task.get("id"):
        report("id must be a non-empty string")
    if task.get("set") not in ("dev", "holdout"):
        report("set must be `dev` or `holdout`")
    if not isinstance(task.get("category"), str) or not task.get("category"):
        report("category must be a non-empty string")
    if not isinstance(task.get("question"), str) or not task.get("question"):
        report("question must be a non-empty string")
    expected = task.get("expected_json")
    if not isinstance(expected, dict) or "answer" not in expected:
        report("expected_json must be an object containing `answer`")
    alternates = task.get("rejected_alternates")
    if not isinstance(alternates, list) or not alternates or not all(
            isinstance(value, str) and value for value in alternates):
        report("rejected_alternates must be a non-empty list of strings")
    evidence = task.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        report("evidence must be a non-empty list of source anchors")
    else:
        for position, anchor in enumerate(evidence):
            if not isinstance(anchor, dict):
                report(f"evidence[{position}] must be an object")
                continue
            relative, snippet = anchor.get("path"), anchor.get("contains")
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or \
                    ".." in Path(relative).parts:
                report(f"evidence[{position}].path must be a corpus-relative path")
                continue
            source = corpus / relative
            if not source.is_file():
                report(f"evidence[{position}] names a missing file: {relative}")
                continue
            if not isinstance(snippet, str) or not snippet:
                report(f"evidence[{position}].contains must be a non-empty string")
            elif snippet not in source.read_text(encoding="utf-8", errors="ignore"):
                report(f"evidence[{position}] snippet is absent from {relative}")
    if not isinstance(task.get("author_notes"), str) or not task.get("author_notes"):
        report("author_notes must be a non-empty string")
    if task.get("acceptable_symbols"):
        report("acceptable_symbols is unsupported: the frozen grader cannot score any-of answers")
    return problems


# Test-file conventions across the six indexed families: Go `_test.go`, Python `test_*.py` and
# `*_test.py`, Java `*Test.java`, JS/TS `*.test.ts` and `*.spec.js`, and any `tests/` directory.
TEST_FILE = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|"
                       r"(_test|[._-]test|[._-]spec|Test|Tests)\.[^/.]+$")


def qualified(identity):
    return isinstance(identity, str) and "::" in identity


def prose(identities):
    """The same answer a model would write as a sentence."""
    names = [identity.split("::")[-1] for identity in identities]
    paths = sorted({identity.split("::")[0] for identity in identities})
    return f"The answer is {', '.join(names)} (in {', '.join(paths)})."


def check_question(task, index, counts, corpus):
    """Every failed expectation for one question, as (severity, detail) pairs."""
    gold = task["expected_json"]["answer"]
    problems = []
    report = lambda severity, detail: problems.append({"severity": severity, "detail": detail})
    identities = quality_pass.flatten(gold)
    question = task["question"]
    for identity in identities:
        if qualified(identity):
            leaf = identity.split("::")[-1]
            if re.search(rf"(?<!\w){re.escape(leaf)}(?!\w)", question):
                report("answerability", f"question leaks target identifier {leaf!r}")
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
        # With namesakes present, the frozen grader can credit only a path-qualified answer.
        if len(defined) > 1 and "::" not in task["question"] and \
                "qualified" not in task["question"]:
            report("answerability",
                   f"{leaf} is defined {len(defined)} times and the question does not ask for "
                   f"a qualified symbol, so a correct bare answer cannot score")

    def leafwise(identity):
        """path::Class::method and path::method name the same definition to a syntactic index."""
        path, _, rest = identity.partition("::")
        return f"{path}::{rest.split('::')[-1]}"

    # A dict gold can assert more than one kind of fact. `caller_key` names the part that is a
    # caller set, so the rest - a container, an implementation - is checked as a definition
    # without being demanded to call anything.
    caller_key = task.get("caller_key")
    if caller_key is not None and not (isinstance(gold, dict) and caller_key in gold):
        report("schema", f"caller_key {caller_key!r} names no key of this gold")
    caller_identities = (quality_pass.flatten(gold[caller_key])
                         if caller_key is not None and isinstance(gold, dict) and caller_key in gold
                         else identities)

    # A gold that claims to enumerate every definition of one name is checked against the index
    # rather than trusted, exactly as a caller set is.
    if task.get("definition_set"):
        leaves = {identity.split("::")[-1] for identity in identities if qualified(identity)}
        if len(leaves) != 1:
            report("gold", "definition_set gold must enumerate one name, not "
                           f"{sorted(leaves)}")
        else:
            leaf = leaves.pop()
            claimed = {identity.split("::")[0] for identity in identities}
            defined = index.get(leaf, set())
            if claimed != defined:
                report("gold", f"gold enumerates {len(claimed)} definitions of {leaf}; the corpus "
                               f"defines it in {sorted(defined)}")

    helper = task.get("helper")
    hops = task.get("hops", 1)
    if hops not in (1, 2):
        report("schema", "hops must be 1 or 2; deeper claims cannot be verified from source alone")
    if helper and qualified(helper):
        path, name = helper.split("::", 1)
        helper_leaf = name.split("::")[-1]
        if path not in index.get(helper_leaf, set()):
            report("gold", f"helper places {helper_leaf} in {path}, but the corpus does not")
        # A suite may ask the question a user actually asks - "who calls this" - by declaring
        # `include_defining_file`, in which case nothing is excluded and the prose has no
        # convention to explain. The default stays the repository's convention.
        whole = bool(task.get("include_defining_file"))
        direct = audit_failures.true_callers(corpus, helper_leaf, path, include_defining_file=whole)
        # A definition is not a caller of itself. `claimed` has always dropped the helper, so
        # `verified` drops it too: with the defining file in scope, `ClientHandshakeInfoFromContext`
        # calling the internal namesake `icredentials.ClientHandshakeInfoFromContext` on its own
        # second line would otherwise make every gold for it non-exhaustive.
        if hops == 1:
            verified = {leafwise(entry) for entry in direct} - {leafwise(helper)}
        else:
            # A transitive claim is verified one independent hop at a time: everything that calls
            # something that calls the helper, each hop enumerated by ripgrep and attributed to its
            # enclosing definition, each scoped exactly as hop one is.
            verified = set()
            for entry in direct:
                caller_path, caller_name = entry.split("::", 1)
                verified |= {leafwise(reached) for reached in
                             audit_failures.true_callers(corpus, caller_name.split("::")[-1],
                                                         caller_path, include_defining_file=whole)}
        # A caller set is a set of identities, and `path::name` is the only identity the frozen
        # grader can compare. Where one file defines that name several times - four methods called
        # `GetRequestMetadata` on four receivers in one Go file - the distinct callers collapse into
        # one gold entry, so an answer that correctly enumerates them all is scored as extras and
        # every arm loses credit for being right. That cost three arms 0.571 each on a question they
        # had answered exactly, so it is a build failure rather than a thing a pilot rediscovers.
        for identity in {leafwise(entry) for entry in direct}:
            entry_path, entry_leaf = identity.split("::", 1)
            if counts.get(entry_leaf, {}).get(entry_path, 0) > 1:
                report("answerability",
                       f"{entry_path} defines {entry_leaf} "
                       f"{counts[entry_leaf][entry_path]} times, so the caller set cannot name "
                       f"which one calls {name}; an exhaustive answer is penalised as extras")
        # Whether a test function counts as a caller is a convention the corpus cannot settle, and
        # a question that leaves it open is scored by a coin flip: across two repetitions of the Go
        # study every arm named 64 of 72 test callers - the same 89% - and which questions the
        # misses landed on decided the quality column. If the verified callers include test files,
        # the question has to say whether they count.
        if any(TEST_FILE.search(entry.split("::")[0]) for entry in direct) and \
                "test" not in task["question"].lower():
            report("answerability",
                   f"callers of {name} include test files and the question does not say whether "
                   f"they count; the gold decides silently and every arm is graded on a guess")
        # An exclusion clause is read through the reader's own conventions, and in Go `foo.go` and
        # `foo_test.go` are one unit: told to skip "the file that defines it", every arm skipped
        # the twin as well, which is fourteen of the nineteen missing identities in the
        # 2026-09-15 study and six of six for one arm. A question that excludes a file while its
        # callers include that file's test twin has to name the twin, or exclude nothing.
        twins = {entry.split("::")[0] for entry in direct
                 if Path(entry.split("::")[0]).name.startswith(Path(path).stem + "_test.")
                 or Path(entry.split("::")[0]).name == "test_" + Path(path).name}
        excludes = re.search(r"outside (of )?(the|its own) file|exclud\w+ the file|only calls .{0,40}outside",
                             task["question"], re.I)
        if whole and excludes:
            report("answerability",
                   "the gold counts callers in the defining file, but the question excludes that "
                   "file; one of the two is wrong")
        missing_twin = sorted(name for name in twins if name not in task["question"])
        if excludes and missing_twin:
            report("answerability",
                   f"the question excludes the defining file while {missing_twin[0]} calls "
                   f"{name}; a Go reader counts the twin as part of the file, so the question "
                   f"must name it or exclude nothing")
        claimed = {leafwise(identity) for identity in caller_identities if identity != helper}
        missing = claimed - verified
        if missing:
            report("gold", f"gold claims callers of {name} at {hops} hop(s) that the corpus does "
                           f"not show: {sorted(missing)}")
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
    # A model answers the question it was asked, not the shape the gold happens to use. Two arms
    # once named exactly the gold callers as a bare list against a {"callers": [...]} gold and
    # scored zero, so the suite now proves the identities survive a different container.
    if isinstance(gold, list) and identities:
        if quality_pass.credit({"answer": list(gold)}, gold, index) != 1.0:
            report("grader", "the gold identities keyed into an object do not score 1")
    if isinstance(gold, dict) and len(gold) == 1 and identities:
        bare = next(iter(gold.values()))
        if quality_pass.credit(bare, gold, index) != 1.0:
            report("grader", "the gold identities without this gold's single key do not score 1")
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
    if not isinstance(tasks, list) or not tasks:
        problem = {"severity": "schema", "detail": "suite must be a non-empty JSON array"}
        return {
            "version": "validate-suite-v2",
            "questions": 0,
            "questions_with_problems": 1,
            "problems": 1,
            "by_severity": {"schema": 1},
            "by_category": {},
            "findings": {"<suite>": [problem]},
            "limitations": "No semantic checks ran because the suite shape is invalid.",
        }
    index = quality_pass.definitions(corpus)
    counts = quality_pass.definition_counts(corpus)
    report, failed, seen = {}, 0, set()
    for position, task in enumerate(tasks):
        if not isinstance(task, dict):
            key = f"<question-{position + 1}>"
            problems = [{"severity": "schema", "detail": "question must be an object"}]
        else:
            key = task.get("id") if isinstance(task.get("id"), str) else \
                f"<question-{position + 1}>"
            problems = check_structure(task, corpus)
            duplicate = key in seen
            if duplicate:
                problems.append({"severity": "schema", "detail": f"duplicate id: {key}"})
            seen.add(key)
            if duplicate:
                key = f"{key}#duplicate-{position + 1}"
            expected = task.get("expected_json")
            if isinstance(expected, dict) and "answer" in expected and \
                    isinstance(task.get("question"), str) and \
                    isinstance(task.get("category"), str):
                problems.extend(check_question(task, index, counts, corpus))
        report[key] = problems
        failed += bool(problems)
    return {
        "version": "validate-suite-v2",
        "questions": len(tasks),
        "questions_with_problems": failed,
        "problems": sum(len(value) for value in report.values()),
        "by_severity": dict(Counter(problem["severity"]
                                    for value in report.values() for problem in value)),
        "by_category": dict(Counter(task.get("category", "<invalid>")
                                    if isinstance(task, dict) else "<invalid>"
                                    for task in tasks)),
        "findings": {key: value for key, value in report.items() if value},
        "limitations": "Checks schema, source anchors, gold against the corpus, and the grader "
                       "against synthetic answers. It cannot tell whether a question is "
                       "interesting, and it reads callers syntactically, exactly as the systems "
                       "under test do.",
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
