#!/usr/bin/env python3
"""Freeze the numbers the README publishes, straight out of the archived run reports.

`runs/` is gitignored, so CI cannot see the evidence behind a published figure and for three
releases nothing checked one against the other. That is how the held-out table came to claim 147
tool calls where its own report says 146, and how the indexing counts survived four new Tree-sitter
grammars unchanged. This script writes `published_results.json` - small, committed, and derived
rather than typed - and `check_docs.py` compares every figure in the README's result section
against it on every push.

Run it locally, where `runs/` exists, whenever a published study is added or re-graded:

    python3 experiments/publish_numbers.py --output experiments/published_results.json

Each study names the report it came from and that report's sha256, so a regenerated extract that
disagrees with the committed one says which archive moved. No model calls, no network.
"""
import argparse
import hashlib
import json
from pathlib import Path

# Every study whose numbers appear in README.md, with the report `end_to_end.py` produced for it.
# A study is listed here only once its criteria are decided and its result is published.
STUDIES = {
    "heldout": {
        "reports": ["runs/heldout-final-20260912/end-to-end.json"],
        "model": "claude-sonnet-4-6",
        "questions": 30,
        "repetitions": 1,
        "note": "Sealed held-out Django set, measured once, frozen four-tool surface and grader.",
    },
    "etcd-mixed-rerun": {
        "reports": [f"runs/etcd-mixed-rerun-20260915/report-rep-{rep}.json" for rep in (1, 2, 3)],
        "model": "gpt-5.6-luna",
        "questions": 30,
        "repetitions": 3,
        "note": "Independent etcd client corpus on HEAD; missed its registered quality criterion "
                "by one answer and is published as a miss.",
    },
    "linux-kernel-relations": {
        "reports": ["runs/linux-relations-20260922/end-to-end.json"],
        "model": "gpt-5.6-luna",
        "questions": 21,
        "repetitions": 3,
        "note": "Fifth kernel run, rejected: a page stating which candidate calls which missed "
                "every registered criterion - idle requests 1.95 to 2.03, quality 59 to 56 of 63, "
                "unevidenced answers 2 to 6 - while producing the first negative token gap at "
                "this scale, -5.4%, bought with three answers. Reverted.",
    },
    "linux-kernel-diet": {
        "reports": ["runs/linux-diet-20260921/end-to-end.json"],
        "model": "gpt-5.6-luna",
        "questions": 21,
        "repetitions": 3,
        "note": "Fourth kernel run: de-duplicated payloads rendered to the model as tables. "
                "Quality 59 of 63 against native's 63, input tokens +1.0% where the first study "
                "was +16.4%, calls 2.10 against 3.67, unevidenced answers 2. The registered sign "
                "change was missed; the arms are at parity.",
    },
    "linux-kernel-complete": {
        "reports": ["runs/linux-complete-20260921/end-to-end.json"],
        "model": "gpt-5.6-luna",
        "questions": 21,
        "repetitions": 3,
        "note": "Third kernel run: C type references refused as definitions, which took one-call "
                "completeness from 2 of 21 to 11 of 21 offline. Calls fell 3.20 to 2.50 and this "
                "arm's own tokens fell 7%, but native fell 8.6% on an identical configuration, "
                "so the same-day gap widened to +10.1% and the token criterion is a miss.",
    },
    "linux-kernel-scanfix": {
        "reports": ["runs/linux-scanfix-20260920/end-to-end.json"],
        "model": "gpt-5.6-luna",
        "questions": 21,
        "repetitions": 3,
        "note": "The same kernel plan with one variable changed - rarity-weighted candidate "
                "selection. Every question predicted to recover did; the token gap against "
                "native halves to +8.3%; two of three registered criteria still missed.",
    },
    "linux-kernel": {
        "reports": ["runs/linux-agent-20260919/end-to-end.json"],
        "model": "gpt-5.6-luna",
        "questions": 21,
        "repetitions": 3,
        "note": "Linux 6.12, 86,602 files - two orders of magnitude past any previous corpus. "
                "Missed all three registered criteria: quality 56 against 62, input tokens "
                "+16.4% rather than -20%, and seven answers whose gold identity never appeared "
                "in a payload. Published as the boundary of the claim.",
    },
}
# One definition, because there is only one true total. Each archived row's `input` is already the
# whole context the model was sent: Codex reports `input_tokens` inclusive of the cached prefix
# (verified per request against `requests.json` and `last_token_usage`, where
# `cached_input_tokens` is a subset), and `end_to_end.steps_from` folds Claude's three separately
# billed fields into the same field. Adding `cache_read` on top counts the cached prefix twice;
# the etcd study was published at -44.6% that way and is -42.4% measured once.
CONTEXT = ("input",)
# What each study is a measurement *of*. A percentage on its own is not a result: the estimand is
# client x corpus x suite x question class, and every reversal in this record was a condition
# being dropped rather than a number being wrong - the claim reversed at kernel scale, and native
# drifted 8.6% between two runs of an identical configuration. Each field is declared here and
# cross-checked against the run directory by `conditions()`, which refuses to publish a
# declaration the archive contradicts; `check_docs.py` refuses a study that omits one. Declared
# and unflattering publishes. Undeclared does not.
#
# `suite_authored` records law 7, and it reads "in-loop" everywhere: every suite in
# `experiments/suites/` was written by an agent session inside this repository, holding these
# working notes and a routing guide for the four tools under test. It is declared rather than
# derived because no run directory records who wrote the questions.
CONDITIONS = {
    "heldout": {
        "client": "claude 2.1.261",
        "corpus": "Django, 276 files",
        "question_class": "mixed shapes, sealed before the run",
        "suite_authored": "in-loop",
        "prompt_documents": "excluded",
        "prompt_document_effect": None,
    },
    "etcd-mixed-rerun": {
        "client": "codex-cli 0.154.0",
        "corpus": "etcd client, 311 files",
        "question_class": "mixed shapes",
        "suite_authored": "in-loop",
        "prompt_documents": "present in both arms",
        "prompt_document_effect": "Not reconstructed: the sessions were ephemeral, so no rollout "
                                  "log sizes the prefix per trial. The document grew from 5.8 KB "
                                  "to 19.5 KB over the record, so its size is an uncontrolled "
                                  "term in every Codex token figure here. A second operator "
                                  "document also reached this study, by being fetched rather than "
                                  "prefixed: all 270 trials of each etcd run, in all three arms, "
                                  "spent a shell call reading "
                                  "~/.agents/skills/retrieval-mcp/SKILL.md (4,253 B), a routing "
                                  "guide that names the four tools under test and calls them "
                                  "'cheaper and more precise than reading files at random'. "
                                  "Exposure was symmetric; usefulness was not, since only one arm "
                                  "has the tools it describes, so the likely direction is to "
                                  "widen the measured gap. The magnitude is not recoverable from "
                                  "the archive. The held-out Django study (0 of 90) and every "
                                  "kernel run from linux-scanfix onward (0 of 126) are clean.",
    },
    "linux-kernel-relations": {
        "client": "codex-cli 0.154.0",
        "corpus": "Linux 6.12, 86,602 files",
        "question_class": "mixed shapes, kernel scale",
        "suite_authored": "in-loop",
        "prompt_documents": "present in both arms",
        "prompt_document_effect": "Reconstructed from the rollout logs: 20,467 tokens of 232,392 "
                                  "for this server and 19,533 of 246,327 for native. Removing it "
                                  "moves the published -5.4% to about -4.6%.",
    },
    "linux-kernel-diet": {
        "client": "codex-cli 0.154.0",
        "corpus": "Linux 6.12, 86,602 files",
        "question_class": "mixed shapes, kernel scale",
        "suite_authored": "in-loop",
        "prompt_documents": "present in both arms",
        "prompt_document_effect": "Reconstructed from the rollout logs: 19,686 tokens of 247,623 "
                                  "for this server and 18,200 of 246,841 for native. Removing it "
                                  "moves the published +1.0% to about +1.6%.",
    },
    "linux-kernel-complete": {
        "client": "codex-cli 0.154.0",
        "corpus": "Linux 6.12, 86,602 files",
        "question_class": "mixed shapes, kernel scale",
        "suite_authored": "in-loop",
        "prompt_documents": "present in both arms",
        "prompt_document_effect": "Not reconstructed: the sessions were ephemeral. The 8.6% "
                                  "native drift this study reports against an identical "
                                  "configuration sits inside the same uncontrolled term.",
    },
    "linux-kernel-scanfix": {
        "client": "codex-cli 0.154.0",
        "corpus": "Linux 6.12, 86,602 files",
        "question_class": "mixed shapes, kernel scale",
        "suite_authored": "in-loop",
        "prompt_documents": "present in both arms",
        "prompt_document_effect": "Not reconstructed: the sessions were ephemeral. From "
                                  "2026-09-20 the document also names the symbol one caller "
                                  "question describes, which is one answer of 63.",
    },
    "linux-kernel": {
        "client": "codex-cli 0.154.0",
        "corpus": "Linux 6.12, 86,602 files",
        "question_class": "mixed shapes, kernel scale",
        "suite_authored": "in-loop",
        "prompt_documents": "present in both arms",
        "prompt_document_effect": "Not reconstructed: the sessions were ephemeral. This is the "
                                  "launch on which the fetched-document leak was caught and "
                                  "closed mid-arc: 7 of its 181 trials (2 native, 5 retrieval) "
                                  "still opened ~/.agents/skills/retrieval-mcp/SKILL.md before "
                                  "codex_wrapper.py gave each trial its own HOME. Every kernel "
                                  "study after it is clean.",
    },
}
# Trial records under these are not the study: a rehearsal, a chunk discarded for a defect found
# mid-run, or one that died on an expired credential. `runs/linux-agent-20260919` keeps all three
# beside its 126 scored trials. The list is a guess about directory names, so `conditions()`
# checks the launches it survives against the trial count the report itself publishes.
EXCLUDED_TRIALS = ("dry-run", "discarded", "partial")
# 0.5 ** 5 = 0.031. Four one-directional discordant questions cannot reach one-sided significance
# at any suite size, so a smaller gap is "not separated" rather than "equivalent".
SIGN_TEST_FLOOR = 5


def trial_launches(run):
    """Every archived launch of this study, read back rather than remembered."""
    for record in sorted(run.glob("**/trial-*/run.json")):
        parts = record.relative_to(run).parts
        if any(part.startswith(EXCLUDED_TRIALS) for part in parts):
            continue
        yield json.loads(record.read_text(encoding="utf-8")).get("agent_command") or []


def carries_document(path):
    """Did this rollout arrive with a project document in its first user message?"""
    with path.open(encoding="utf-8", errors="replace") as handle:
        return any("AGENTS.md instructions for" in line for line in handle)


def prompt_document_evidence(runs):
    """Could a project document reach the model in this study, and did one?

    Two readings, because they fail differently. The launch says whether the client was told to
    exclude one - `claudeMdExcludes` for Claude, `project_doc_max_bytes=0` for Codex. The rollout
    log says what was actually sent, and only a non-ephemeral session keeps one, which in this
    record is the last two kernel studies. The rollout wins where it exists: the document arrives
    as text in the first user message, so no flag and no tool-call detector can stand in for it.
    """
    launches = [" ".join(command) for run in runs for command in trial_launches(run)]
    suppressed = sum(1 for command in launches
                     if "claudeMdExcludes" in command or "project_doc_max_bytes=0" in command)
    rollouts = [path for run in runs for path in sorted(run.glob("**/codex-session.jsonl"))]
    carrying = sum(1 for path in rollouts if carries_document(path))
    if carrying or (launches and suppressed < len(launches)):
        derived = "present in both arms"
    elif launches:
        derived = "excluded"
    else:
        derived = "no archived launch"
    return {"derived": derived, "launches": len(launches),
            "launches_suppressing_the_document": suppressed, "rollouts_read": len(rollouts),
            "rollouts_carrying_a_document": carrying}


def quality_separation(arms):
    """Can this suite tell the arms apart on quality at all?

    The control saturates - 89 of 90 on etcd, 62 and 63 of 63 at kernel scale - so "quality ties"
    is a failure to separate and not evidence of equivalence, and it has been registered as a
    primary outcome anyway. A paired sign test needs `SIGN_TEST_FLOOR` discordant questions all in
    one direction before it can reach p < 0.05, whatever n is. Only the net difference survives
    into this extract and the net is a lower bound on discordance, so `separable` is the
    optimistic reading: false here means the suite could not have shown a difference.
    """
    control, treatment = arms.get("native-control"), arms.get("retrieval-mcp")
    if not control or not treatment:
        return None
    net = control["correct"] - treatment["correct"]
    return {"control_correct": control["correct"], "control_trials": control["trials"],
            "control_headroom": control["trials"] - control["correct"],
            "net_resolved_difference": net,
            "discordant_needed_for_one_sided_05": SIGN_TEST_FLOOR,
            "separable": abs(net) >= SIGN_TEST_FLOOR,
            "reading": "separated" if abs(net) >= SIGN_TEST_FLOOR else
                       "not separated: an absence of power, not equivalence"}


def conditions(repo, name, spec, arms):
    """The conditions this study's figures are true under, declared and then checked."""
    declared = CONDITIONS.get(name)
    if declared is None:
        raise SystemExit(f"{name} publishes figures with no conditions declared; add them to "
                         f"CONDITIONS before any of its numbers can be quoted")
    evidence = prompt_document_evidence(sorted({(repo / report).parent
                                                for report in spec["reports"]}))
    if evidence["derived"] not in (declared["prompt_documents"], "no archived launch"):
        raise SystemExit(f"{name} declares prompt documents {declared['prompt_documents']!r} and "
                         f"its own archive says {evidence['derived']!r}: {evidence}")
    scored = sum(arm["trials"] for arm in arms.values())
    if evidence["launches"] and evidence["launches"] != scored:
        raise SystemExit(f"{name} scored {scored} trials and {evidence['launches']} archived "
                         f"launches were read; the evidence covers a different set of trials "
                         f"than the figures do")
    return {**declared, "prompt_document_evidence": evidence,
            "quality_separation": quality_separation(arms)}


def tokens(row, fields):
    usage = row.get("tokens") or {}
    return sum(usage.get(field) or 0 for field in fields)


def aggregate(rows):
    """One arm's published totals. Means are reported where the README prints a mean."""
    hits = [row["calls_to_first_hit"] for row in rows if row.get("calls_to_first_hit") is not None]
    return {
        "trials": len(rows),
        "correct": sum(1 for row in rows if row["resolved_correct"]),
        "input_tokens": sum(tokens(row, CONTEXT) for row in rows),
        "calls": sum(row.get("calls") or 0 for row in rows),
        "context_token_turns": sum(row.get("context_token_turns") or 0 for row in rows),
        "calls_to_first_evidence": round(sum(hits) / len(hits), 3) if hits else None,
        "answered_without_evidence": sum(1 for row in rows if row["answered_without_evidence"]),
    }


def study(repo, name, spec):
    arms, reports = {}, []
    for relative in spec["reports"]:
        path = repo / relative
        data = json.loads(path.read_text(encoding="utf-8"))
        reports.append({"report": relative,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for row in data["rows"]:
            if row["status"] != "completed":
                raise ValueError(f"{relative} holds an unfinished trial; score it or exclude it")
            arms.setdefault(row["system"], []).append(row)
    totals = {arm: aggregate(rows) for arm, rows in sorted(arms.items())}
    return {"model": spec["model"], "questions": spec["questions"],
            "repetitions": spec["repetitions"], "note": spec["note"], "sources": reports,
            "conditions": conditions(repo, name, spec, totals), "arms": totals}


def build(repo):
    return {"version": "published-results-v1",
            "definitions": {"input_tokens": "whole context sent per request, cached prefix "
                                            "counted once",
                            "conditions": "what each study is a measurement of - client, corpus, "
                                          "question class, who authored the suite, and what "
                                          "reached the prompt besides the question. Declared, "
                                          "then checked against the run directory."},
            # Figures the result section publishes that are not comparisons between arms of a run.
            # They are declared here with their provenance so `check_docs.py` can tell "measured
            # somewhere else" from "invented", and an undeclared percentage fails the build.
            "declared_percentages": {
                "-13.3": "0.1.6 routing filter: handshake instructions 2,724 -> 2,361 characters, "
                         "measured from src/tools/mod.rs. Prompt-prefix bytes, not a run result.",
                "-36.2": "Withdrawn 2026-09-22. The held-out study's second token column added "
                         "cache reads to a total that already contained them; the measured "
                         "figure is -33.5%. Named in the README so the correction is legible.",
                "-44.6": "Withdrawn 2026-09-22. The etcd study was published at this figure from "
                         "the same double count; the measured figure is -42.4%.",
                "-42.4": "Withdrawn 2026-09-23, and not to a corrected number. Every trial of "
                         "the etcd study, in all three arms, fetched "
                         "~/.agents/skills/retrieval-mcp/SKILL.md - a routing document naming "
                         "these four tools and calling them cheaper and more precise than "
                         "reading files at random. runs/cli-transport-20260924 re-ran the same "
                         "corpus fingerprint, suite sha and client with the document absent and "
                         "measured +96.3%; runs/client-version-20260924 repeated it on the "
                         "study's own client version and measured +71.0%; "
                         "runs/leak-price-20260924 seeded the document back into one arm and "
                         "moved it from 166,988 input tokens a trial to 104,200, within 7% of "
                         "the archived arm's 111,389. The document accounts for the published "
                         "result rather than contributing to it, so the figure is withdrawn "
                         "rather than corrected.",
                "-26.8": "Withdrawn 2026-09-23 alongside the -42.4% it accompanied. The etcd "
                         "study's zvec-grep comparison was measured under the same uncontrolled "
                         "document, in the same trials.",
            },
            "studies": {name: study(repo, name, spec) for name, spec in STUDIES.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).with_name("published_results.json"))
    args = parser.parse_args()
    extract = build(args.repo.resolve(strict=True))
    args.output.write_text(json.dumps(extract, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output),
                      "studies": {name: sorted(data["arms"]) for name, data in
                                  extract["studies"].items()}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
