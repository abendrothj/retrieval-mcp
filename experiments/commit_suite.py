#!/usr/bin/env python3
"""Mine a question set from upstream commits, with no agent anywhere in the loop.

Every suite in `experiments/suites/` was authored by an agent session inside this repository,
which means it was written with `AGENTS.md` - a document naming the claim, the arms and the
expected direction - already in context. The golds survive that, because the oracles are ripgrep
and ctags. Which questions exist, and how they are worded, do not.

An isolated authoring session is the usual answer. This is a better one: there is no authoring
session at all. A commit that upstream wrote years before this project existed cannot have been
worded to favour an arm here.

    Question = an upstream commit's message, at a revision after the corpus snapshot.
    Gold     = the enclosing definitions that commit's diff modified, attributed exactly the way
               `study_b.attribution` attributes a retrieved row, and required to still exist at
               the pinned revision the agent will actually search.

**Every filter here references the corpus or the commit, never an arm.** No merges, no
whitespace-only changes, source suffixes only, a gold that exists at the pinned revision, few
enough touched definitions to grade, a message length band, and a message that does not spell the
answer. That last one is not a taste judgement: `validate_suite.py` refuses a question leaking a
target identifier, so a commit whose subject names the function it changed cannot become a
question whatever anyone thinks of it.

**What is recorded rather than cut.** `names_target` - whether the message shares a word piece
with the target's own name - is a covariate, because 16 of 21 kernel and 16 of 30 etcd questions
already carry one and deleting them would bias the sample rather than clean it. So is
`gold_cardinality`, which `runs/page-position-20260922` showed is what decides whether a page-cut
positive control can cost anything: single-identity golds are at risk 6% of the time and
multi-identity ones 66%. Stratify on these and report them; do not select on them.

`lexical_oracle.py` and `tool_reachability.py` take a finished suite and a server binary, so they
are the next step in the pipeline rather than part of this one - and their results are to be
recorded beside the suite, not used to cut it. Cutting questions with an oracle that runs inside
the loop is one of the sample-biasing filters already in the current suites.

**The wording is one constant.** Each question is the cleaned upstream message followed by a
fixed sentence, identical for every question in the set, so no per-question phrasing decision is
made by anything in this repository. The two variants differ only by whether the gold names one
definition or several.

Output is a suite in the schema `validate_suite.py` compiles, plus a covariate block. Compile it
with `validate_suite.py` before using it: the namesake, container and caller guards still apply
and nothing here replaces them.

    commit_suite.py --corpus corpora/gson --revision v2.11.0 --output gson_commit_questions.json

No model, no network - the history has to be in the clone already.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import tempfile

import quality_pass
import study_b

# A commit message's trailers say who signed it off, not what it did.
TRAILER = re.compile(
    r"^\s*(signed-off-by|co-authored-by|reviewed-by|acked-by|tested-by|reported-by|suggested-by|"
    r"cc|fixes|closes|close|resolves|link|bug|change-id|reviewed-on|differential revision|"
    r"pull-request|pr|see also|refs?)\s*[:#]", re.IGNORECASE)
URL = re.compile(r"https?://\S+")
# `@@ -12,7 +12,9 @@` - the pre-image side is the one that has definitions at the parent commit.
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")
# camelCase, snake_case and SCREAMING_CASE all split into the pieces a message might echo.
PIECE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")
CATEGORY = "commit_change_locus"
# Test code is excluded from gold. "Which test did this commit touch" is not a question about
# what the corpus does, and a suite of them would measure a retrieval surface against a part of
# the tree no user asks about. The filter reads the corpus and never an arm, so it belongs to the
# allowed class.
TEST_PATH = re.compile(r"(^|/)(tests?|testing|spec|specs|__tests__)(/|$)")
TEST_FILE = re.compile(r"(^|[._-])(test|spec)s?[._]|_test\.|\.test\.|\.spec\.", re.IGNORECASE)
TEST_NAME = re.compile(r"^(test[_A-Z]|Test[A-Z]|should[A-Z])")
# Known ceiling: a Rust test lives in `#[cfg(test)] mod tests` in the same file and is named
# freely, so no path or name rule sees it. Everything declared after the first `#[cfg(test)]` in
# a file is treated as test code, which is right for the conventional trailing test module and
# wrong for a file that puts one in the middle. The upgrade path is asking the symbol index for
# the enclosing module instead of scanning for the marker.
TEST_SCOPE = re.compile(r"^\s*#\[cfg\(test\)\]")
# Maintenance commits describe tooling, not the repository's behaviour, so nothing in the corpus
# implements what they say and no arm can find it. The first smoke drew "Bump
# com.google.errorprone:error_prone_core from 2.20.0 to 2.21.1" keyed to `TypeAdapters.java::read`
# and all four arms answered differently and wrongly: a question that measures nothing but costs a
# trial. The class is a property of the commit message, never of an arm.
#
# Researcher degree of freedom, stated rather than hidden: these patterns were written after
# reading this corpus's own subject lines. A different corpus should re-derive them rather than
# inherit them, and the count they remove is reported.
MAINTENANCE = re.compile(
    r"^\s*(bump|upgrade|update)\b.*\bfrom\b.*\bto\b"
    r"|^\s*(bump|chore|ci|build|docs?|style)\b\s*[:(]"
    r"|suppress\w*\s+(a\s+couple\s+of\s+)?\w*\s*warnings?"
    r"|error[\s-]?prone"
    r"|\bmigrate\s+(all\s+)?tests?\b"
    r"|\bavoid\b.*\b(warning|check|issue)\b"
    r"|\brestructure\b.*\bwarning\b"
    r"|\badd\s+build\s+config\b"
    r"|\btroubleshooting\s+guide\b"
    r"|\bclarifying\s+parentheses\b"
    r"|\bjavadoc\b|\bchangelog\b|\breadme\b",
    re.IGNORECASE)
# One constant per gold shape, so no per-question wording decision is made in this repository.
ONE = ("Name the function or method in this corpus that implements the behaviour described "
       "above, as path::name.")
MANY = ("Name every function or method in this corpus that the change described above modified, "
        "each as path::name.")


def git(repo, *arguments):
    """One git command against the corpus clone, as text. Raises on a nonzero exit."""
    done = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True,
                          errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {done.stderr.strip()}")
    return done.stdout


def commits_after(repo, revision, limit, newest_first=False, until="HEAD"):
    """Non-merge commits in `revision..until`, nearest the pinned snapshot first.

    Which end is nearest depends on which end the snapshot is, and both are real cases:

      * The corpus is pinned at `revision` and HEAD is a later tip. The commits describe changes
        the corpus does not have yet, and the oldest is nearest the snapshot - the default.
      * The corpus is pinned at HEAD and `revision` is an ancestor. The commits describe changes
        the corpus already contains, which makes the described behaviour actually present in the
        tree the agent searches, and the newest is nearest.

    Order matters because source drifts away from the snapshot with distance, and a gold that no
    longer exists there is dropped. Mining from the wrong end spends the budget on the commits
    likeliest to be refused.
    """
    out = git(repo, "rev-list", "--no-merges", f"{revision}..{until}")
    found = [line.strip() for line in out.splitlines() if line.strip()]
    if not newest_first:
        found.reverse()
    return found[:limit] if limit else found


def clean_message(text):
    """The prose of a commit message: no trailers, no URLs, no leading/trailing blank lines."""
    kept = [line.rstrip() for line in text.splitlines()
            if not TRAILER.match(line) and not line.strip().startswith("#")]
    body = URL.sub("", "\n".join(kept))
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def touched_paths(repo, commit):
    """Source files this commit modified. Added and deleted files have no pre-image definition."""
    out = git(repo, "show", "--no-color", "--format=", "--name-status", "-M", commit)
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("M") and parts[-1].endswith(
                study_b.SOURCE_SUFFIXES):
            found.append(parts[-1])
    return found


def preimage_lines(repo, commit, path):
    """Pre-image line numbers this commit's diff changed, ignoring whitespace-only hunks.

    `-w` is what makes a reindentation or a brace move stop producing questions: with it, a
    whitespace-only change yields no hunks at all and the file drops out.
    """
    out = git(repo, "show", "--no-color", "--format=", "--unified=0", "-w", commit, "--", path)
    found = []
    for line in out.splitlines():
        hunk = HUNK.match(line)
        if hunk:
            start = int(hunk.group(1))
            count = int(hunk.group(2)) if hunk.group(2) is not None else 1
            found.extend(range(start, start + max(count, 1)))
    return found


def definitions_touched(repo, commit, path, workspace):
    """The enclosing definitions this commit changed in one file, by leaf name.

    Attribution is `study_b.attribution`, the same function that decides what a retrieved row
    points at, so a gold and a hit are resolved by one rule rather than two. It reads a file from
    disk, so the parent's version of the file is materialised under `workspace` with its own
    suffix - the suffix is what selects the language table.
    """
    lines = preimage_lines(repo, commit, path)
    if not lines:
        return set()
    content = git(repo, "show", f"{commit}^:{path}")
    scratch = workspace / Path(path).name
    scratch.write_text(content, encoding="utf-8")
    found = set()
    for line in lines:
        name = study_b.attribution(scratch, line)
        if name:
            found.add(name)
    return found


def is_test_code(corpus, path, leaf):
    """Whether this definition is test code, by path, by file name, by leaf, or by test scope."""
    if TEST_PATH.search(path) or TEST_FILE.search(Path(path).name) or TEST_NAME.match(leaf):
        return True
    source = (corpus / path).read_text(encoding="utf-8", errors="ignore").splitlines()
    marker = next((number for number, text in enumerate(source) if TEST_SCOPE.match(text)), None)
    if marker is None:
        return False
    declared = next((number for number, text in enumerate(source)
                     if study_b.leading_declaration(corpus / path, text) == leaf), None)
    return declared is not None and declared > marker


def word_pieces(name):
    return {piece.lower() for piece in PIECE.findall(name) if len(piece) > 2}


def names_target(message, identities):
    """Whether the message echoes a word piece of a target's own name. A covariate, not a filter.

    16 of 21 kernel and 16 of 30 etcd questions already carry one. Deleting them would bias the
    sample; reporting them lets a study stratify on the thing that actually varies.
    """
    words = word_pieces(message)
    return any(word_pieces(identity.split("::")[-1]) & words for identity in identities)


def leaks(message, identities, defined_names=None):
    """Whether the message hands over the answer, by symbol or by the file that holds it.

    `validate_suite.py` refuses a leaked target identifier, so the leaf check is mandatory. The
    file check is here because the smoke found the other half: "Fix `RuntimeTypeAdapterFactory`
    depending on internal `Streams` class" names the gold's own file, which leaves only "which
    method", and several arms answered it with zero retrieval calls. A question any arm can
    answer without retrieving measures nothing about retrieval and makes every arm look alike.

    A file stem only counts when the corpus actually defines a symbol of that name. In Java the
    stem is the class - "RuntimeTypeAdapterFactory" hands over the location - but in Python a
    stem is usually a generic module word, and treating `parser.py` as a leak refused every
    sqlglot issue that used the word "parser". `defined_names` is the grader's definition index
    when the caller has one; without it the stem is trusted as distinctive, which is the older
    behaviour and the right default for the one-file-per-class languages.
    """
    for identity in identities:
        path, _, _ = identity.partition("::")
        tokens = [identity.split("::")[-1]]
        stem = Path(path).stem
        if defined_names is None or stem in defined_names:
            tokens.append(stem)
        for token in tokens:
            if token and re.search(rf"(?<!\w){re.escape(token)}(?!\w)", message):
                return True
    return False


def declaration_line(corpus, path, leaf):
    """The text of the line that opens this definition, for an evidence anchor."""
    source = (corpus / path).read_text(encoding="utf-8", errors="ignore").splitlines()
    for text in source:
        if study_b.leading_declaration(corpus / path, text) == leaf:
            stripped = text.strip()
            if stripped:
                return stripped
    return None


def alternates(corpus, path, gold_leaves, wanted=3):
    """Plausible wrong answers: other definitions in the same file the commit did not touch."""
    source = (corpus / path).read_text(encoding="utf-8", errors="ignore").splitlines()
    found = []
    for text in source:
        name = study_b.leading_declaration(corpus / path, text)
        if name and name not in gold_leaves and f"{path}::{name}" not in found:
            found.append(f"{path}::{name}")
        if len(found) >= wanted:
            break
    return found


def question_for(commit, message, identities, corpus, prefix, revision):
    """One suite entry, or None with the reason it was refused."""
    gold = sorted(identities)
    leaves = {identity.split("::")[-1] for identity in gold}
    if leaks(message, gold):
        return None, "message spells a target identifier"
    path = gold[0].split("::")[0]
    anchor = declaration_line(corpus, path, gold[0].split("::")[-1])
    if not anchor:
        return None, "no declaration line to anchor evidence on"
    rejected = alternates(corpus, path, leaves)
    if not rejected:
        return None, "no other definition in the file to offer as a wrong answer"
    return {
        "id": f"{prefix}-{commit[:9]}",
        "category": CATEGORY,
        "set": "holdout",
        "question": f"{message}\n\n{ONE if len(gold) == 1 else MANY}",
        "expected_json": {"answer": gold[0] if len(gold) == 1 else gold},
        "rejected_alternates": rejected,
        "evidence": [{"path": path, "contains": anchor}],
        "author_notes":
            f"Derived mechanically by commit_suite.py from upstream commit {commit} against the "
            f"corpus pinned at {revision}; no agent authored the question. The prose is the "
            f"commit's own message with trailers and URLs removed, followed by one constant "
            f"sentence used for every question of this shape. Gold is the set of enclosing "
            f"definitions the diff modified, attributed by study_b.attribution and required to "
            f"exist at the pinned revision. Covariates are recorded in the report beside this "
            f"suite and were not used to select it.",
        # Covariates travel with the question so a study can stratify without re-deriving them.
        "covariates": {
            "commit": commit,
            "gold_cardinality": len(gold),
            "names_target": names_target(message, gold),
            "message_chars": len(message),
        },
    }, None


def build(corpus, revision, prefix, limit, scan, max_definitions, min_chars, max_chars,
          include_tests=False, newest_first=False, until="HEAD"):
    questions, refused = [], Counter()
    considered = mixed = 0
    # One index over the pinned corpus, built by the grader's own definition table.
    graded = quality_pass.definitions(corpus)
    with tempfile.TemporaryDirectory() as raw:
        workspace = Path(raw)
        for commit in commits_after(corpus, revision, scan, newest_first, until):
            if limit and len(questions) >= limit:
                break
            considered += 1
            message = clean_message(git(corpus, "show", "--no-color", "--format=%s%n%n%b",
                                        "--no-patch", commit))
            if not min_chars <= len(message) <= max_chars:
                refused["message outside the length band"] += 1
                continue
            # Subject only. Matching the body killed 131 of 395 commits because a body that
            # merely mentions a README or a javadoc is not a maintenance commit; what the commit
            # *is* about is its subject line.
            if MAINTENANCE.search(message.splitlines()[0] if message else ""):
                refused["maintenance commit: describes tooling, not behaviour"] += 1
                continue
            identities, dropped_tests = set(), 0
            for path in touched_paths(corpus, commit):
                for leaf in definitions_touched(corpus, commit, path, workspace):
                    # The gold has to be answerable against the revision the agent searches,
                    # not against the revision the commit was written on - and "answerable" has
                    # to mean what the *grader* can see. `study_b.defines` deliberately uses a
                    # looser table than `quality_pass.definitions`, so checking gold against it
                    # let through identities validate_suite then refused as not defined where the
                    # gold placed them. The grader's index is the one that decides gradability.
                    if not ((corpus / path).is_file() and path in graded.get(leaf, set())):
                        continue
                    if not include_tests and is_test_code(corpus, path, leaf):
                        dropped_tests += 1
                        continue
                    identities.add(f"{path}::{leaf}")
            if not identities:
                refused["only test code changed" if dropped_tests else
                        "no modified definition survives at the pinned revision"] += 1
                continue
            if dropped_tests:
                # Still usable, but the gold is now the non-test part of the commit, which is a
                # narrower claim than "everything this commit changed". Counted, not hidden.
                mixed += 1
            if len(identities) > max_definitions:
                refused["too many definitions to grade"] += 1
                continue
            entry, why = question_for(commit, message, identities, corpus, prefix, revision)
            if entry is None:
                refused[why] += 1
                continue
            questions.append(entry)
    return questions, refused, considered, mixed


def report(questions, refused, considered, mixed, corpus, revision):
    cardinality = Counter(task["covariates"]["gold_cardinality"] for task in questions)
    return {
        "version": "commit-suite-v1",
        "corpus": str(corpus),
        "revision": revision,
        "commits_considered": considered,
        "questions": len(questions),
        "refused": dict(refused),
        "mixed_commits_whose_test_definitions_were_excluded": mixed,
        "covariates": {
            "gold_cardinality": {str(key): value for key, value in sorted(cardinality.items())},
            "multi_identity_questions": sum(value for key, value in cardinality.items() if key > 1),
            "names_target": sum(1 for task in questions if task["covariates"]["names_target"]),
        },
        "next_steps": [
            "validate_suite.py against the same corpus - the namesake, container and caller "
            "guards still apply and nothing here replaces them",
            "lexical_oracle.py and tool_reachability.py on the compiled suite, recorded beside "
            "it and not used to cut it",
        ],
        "limitations":
            "Gold is what the diff touched, which is the change's locus and not necessarily the "
            "only correct answer to the prose; a commit that fixes a caller of the real culprit "
            "names the caller. Attribution is syntactic, so a change inside a nested closure is "
            "credited to the definition that encloses it. Only modified files are mined: an "
            "added definition does not exist at the pinned revision and a deleted one cannot be "
            "found there. Messages are upstream prose and vary in how much they describe "
            "behaviour at all, which is what the length band and the covariates are for.",
    }


def render(result):
    lines = [f"{result['corpus']} at {result['revision']}: {result['questions']} questions "
             f"from {result['commits_considered']} commits considered"]
    for reason, count in sorted(result["refused"].items(), key=lambda row: -row[1]):
        lines.append(f"  refused {count:>4}  {reason}")
    covariates = result["covariates"]
    lines.append(f"  gold cardinality {covariates['gold_cardinality']}; "
                 f"multi-identity {covariates['multi_identity_questions']}; "
                 f"names_target {covariates['names_target']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, required=True, help="a clone carrying history")
    parser.add_argument("--revision", required=True, help="the pinned corpus snapshot")
    parser.add_argument("--prefix", default="cm", help="question id prefix")
    parser.add_argument("--limit", type=int, default=30, help="questions to emit; 0 for all")
    parser.add_argument("--scan", type=int, default=0, help="commits to examine; 0 for all")
    parser.add_argument("--max-definitions", type=int, default=4)
    parser.add_argument("--min-chars", type=int, default=120)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--include-tests", action="store_true",
                        help="allow test definitions into the gold; off by default")
    parser.add_argument("--until", default="HEAD",
                        help="the far end of the commit range; the corpus checkout stays "
                             "the pinned revision, so mining forward never requires "
                             "checking the far end out")
    parser.add_argument("--newest-first", action="store_true",
                        help="the corpus is pinned at HEAD and --revision is an "
                             "ancestor, so the newest commit is nearest the snapshot")
    parser.add_argument("--output", type=Path, help="the suite")
    parser.add_argument("--report", type=Path, help="the covariate and refusal report")
    args = parser.parse_args()
    corpus = args.corpus.resolve(strict=True)
    questions, refused, considered, mixed = build(
        corpus, args.revision, args.prefix, args.limit, args.scan, args.max_definitions,
        args.min_chars, args.max_chars, args.include_tests, args.newest_first,
        args.until)
    result = report(questions, refused, considered, mixed, corpus, args.revision)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(json.dumps(questions, indent=2) + "\n", encoding="utf-8")
    if args.report:
        if args.report.exists():
            raise FileExistsError(args.report)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(render(result))
    return 0 if questions else 1


if __name__ == "__main__":
    raise SystemExit(main())
