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
# Both definitions this project has used for "input tokens". The narrow one is what the held-out
# table published; the wide one is what the working notes declare. Drift between them was itself a
# defect, so the extract carries both and the README is required to name which it prints.
NARROW = ("input", "cache_creation")
WIDE = ("input", "cache_read", "cache_creation")


def tokens(row, fields):
    usage = row.get("tokens") or {}
    return sum(usage.get(field) or 0 for field in fields)


def aggregate(rows):
    """One arm's published totals. Means are reported where the README prints a mean."""
    hits = [row["calls_to_first_hit"] for row in rows if row.get("calls_to_first_hit") is not None]
    return {
        "trials": len(rows),
        "correct": sum(1 for row in rows if row["resolved_correct"]),
        "input_tokens": sum(tokens(row, NARROW) for row in rows),
        "input_tokens_with_cache_reads": sum(tokens(row, WIDE) for row in rows),
        "calls": sum(row.get("calls") or 0 for row in rows),
        "context_token_turns": sum(row.get("context_token_turns") or 0 for row in rows),
        "calls_to_first_evidence": round(sum(hits) / len(hits), 3) if hits else None,
        "answered_without_evidence": sum(1 for row in rows if row["answered_without_evidence"]),
    }


def study(repo, spec):
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
    return {"model": spec["model"], "questions": spec["questions"],
            "repetitions": spec["repetitions"], "note": spec["note"], "sources": reports,
            "arms": {arm: aggregate(rows) for arm, rows in sorted(arms.items())}}


def build(repo):
    return {"version": "published-results-v1",
            "definitions": {"input_tokens": " + ".join(NARROW),
                            "input_tokens_with_cache_reads": " + ".join(WIDE)},
            # Figures the result section publishes that are not comparisons between arms of a run.
            # They are declared here with their provenance so `check_docs.py` can tell "measured
            # somewhere else" from "invented", and an undeclared percentage fails the build.
            "declared_percentages": {
                "-13.3": "0.1.6 routing filter: handshake instructions 2,724 -> 2,361 characters, "
                         "measured from src/tools/mod.rs. Prompt-prefix bytes, not a run result.",
            },
            "studies": {name: study(repo, spec) for name, spec in STUDIES.items()}}


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
