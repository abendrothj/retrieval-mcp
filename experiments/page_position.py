#!/usr/bin/env python3
"""Where the gold sat on the page the agent was actually served, and what a page cut can cost.

`degrade_server.py` offers three knobs and only two of them can move quality. `--shuffle` permutes
rows and keeps every byte, so an agent that reads the whole page loses nothing and a shuffled arm
can score exactly like HEAD while proving nothing at all. `--page-fraction` is the knob that can
actually remove the answer from the page. Before a positive control is preregistered around it,
the honest question is how many questions it *could* cost - and that is an archive reading, not a
model run: every completed trial recorded the whole payload it received, rows in served order.

So this is the power calculation for the positive control. For every gold identity a question
asserts, it finds the ranked rows that exposed it, and asks whether the leading fraction the knob
keeps would still have contained one. A question whose gold was served at the top of every page is
a question `--page-fraction 0.5` cannot touch, however plausible the arithmetic looks.

Three properties of this reading, all of which push the printed ceiling *down*:

  * **Visibility is textual, and deliberately the same generosity `end_to_end.unretrieved` uses**:
    a row exposes `path::leaf` when the row's own JSON contains both strings. A row that merely
    mentions the name in an excerpt therefore counts as carrying it. Generous visibility finds the
    gold higher up the page, so it can only understate what a cut removes.
  * **Only ranked pages count.** `read_source` returns `lines`, the knob does not touch it, and a
    gold reached only by reading a known location is reported separately rather than as at risk.
  * **Behaviour is held fixed.** A degraded arm asks different follow-up questions, and some of
    those recover. This prices the first-order loss, which is the ceiling, not the effect.

The same pass sizes `--blind-attribution` for free, by re-testing every row with the enclosing-
definition columns removed: that says how many golds were legible *only* because a row named the
definition it belongs to.

    page_position.py --run runs/linux-scanfix-20260920/report \\
        --questions experiments/suites/linux_kernel_questions.json

No model, no network, no corpus: the archived payloads are the whole evidence base.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

import benchmark
import end_to_end

# The bands the headline distribution reports the gold's position in, as a fraction of the page it
# was served on. `--page-fraction F` keeps a leading prefix, so a band is exactly the set of
# questions a cut at its upper edge would begin to threaten.
BANDS = (0.1, 0.25, 0.5, 0.75, 1.0)
# What a registration would plausibly choose. 0.5 is the value the plan named; the rest bracket it
# so an underpowered 0.5 comes with the fraction that would not be.
FRACTIONS = (0.9, 0.75, 0.5, 0.34, 0.25, 0.1)
# `degrade_server.ATTRIBUTION`. Kept as a literal so this instrument runs against a checkout that
# does not carry the proxy; `test_page_position` asserts the two agree wherever both are present.
ATTRIBUTION = ("symbol", "caller")


def kept_rows(count, fraction):
    """How many leading rows `--page-fraction` keeps from a page of `count`.

    This is `degrade_server.orderer`'s arithmetic and it must not drift from it: a ceiling computed
    against a different prefix length is a number about nothing. The test cross-checks it against
    the proxy itself when the proxy is importable.
    """
    return max(1, round(count * fraction))


def ranked_rows(body):
    """The ranked rows of one archived tool payload, in served order, or None if it has none.

    Two clients, three spellings. Codex records `structured_content` beside a `content` list whose
    single text block is the same JSON; Claude records `structuredContent`; and a `tool_result`
    block may arrive as a bare string. All three are tried rather than one being trusted, because
    a payload silently read as "no rows" is a question silently dropped from the denominator.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    for _ in range(2):
        if not isinstance(payload, dict):
            break
        for key in ("structuredContent", "structured_content"):
            inner = payload.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("results"), list):
                return inner["results"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        # Otherwise the rows are inside a text block: `{"content": [{"type": "text", ...}]}` for
        # both clients, or `{"content": "<json>"}` for a Claude result recorded as a string.
        content = payload.get("content")
        if isinstance(content, list):
            content = next((block.get("text") for block in content
                            if isinstance(block, dict) and block.get("type") == "text"), None)
        if not isinstance(content, str):
            break
        try:
            payload = json.loads(content)
        except ValueError:
            break
    return None


def row_text(row, blind=False):
    """One row as the text the visibility test reads, optionally with attribution removed."""
    if blind:
        row = {key: value for key, value in row.items() if key not in ATTRIBUTION}
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def exposes(text, pair):
    """`end_to_end.unretrieved`'s test, applied to one row instead of a whole session."""
    return pair[0] in text and pair[1] in text


def trial_pages(records):
    """(rows, ...) for every ranked payload this trial received, in call order."""
    return [rows for rows in (ranked_rows(record["body"]) for record in records) if rows]


def sightings(pages, pair, blind=False):
    """Per page that exposed this identity, (position, page length). Position is 1-based.

    Only the first exposing row of a page is recorded: a prefix that keeps it keeps the identity,
    and the rows behind it change nothing about survival.
    """
    found = []
    for rows in pages:
        for position, row in enumerate(rows, 1):
            if exposes(row_text(row, blind), pair):
                found.append((position, len(rows)))
                break
    return found


def survives(seen, fraction):
    return any(position <= kept_rows(count, fraction) for position, count in seen)


def best_share(seen):
    """The gold's best position as a fraction of its page - the number the bands sort on."""
    return min(position / count for position, count in seen) if seen else None


def trial_reading(trial, task):
    """One trial: per gold identity, where it was served and what would still reach the model."""
    stream, client = end_to_end.events(trial)
    if client is None:
        return None
    records = end_to_end.calls_from(stream, client)
    pages = trial_pages(records)
    pairs = end_to_end.gold_identities(task)
    everywhere = "\n".join(record.get("request", "") + "\n" + record["body"]
                           for record in records)
    identities = {}
    for pair in pairs:
        seen = sightings(pages, pair)
        identities["::".join(pair)] = {
            "on_ranked_page": bool(seen),
            # An identity the session saw only through `read_source` or a shell command is not
            # something a page cut removes; counting it as at risk would inflate the ceiling.
            "seen_anywhere": exposes(everywhere, pair),
            "best_share": best_share(seen),
            "sightings": [{"position": position, "page_rows": count} for position, count in seen],
            "survives": {f"{fraction:g}": survives(seen, fraction) for fraction in FRACTIONS},
            # The other knob that bites, from the same rows: would this identity still be legible
            # if the enclosing-definition columns were gone?
            "survives_blind": bool(sightings(pages, pair, blind=True)),
        }
    return {"pages": len(pages), "page_rows": [len(rows) for rows in pages],
            "identities": identities}


def question_reading(readings):
    """Aggregate one question's trials. A question is at risk when a trial loses an identity."""
    at_risk = {}
    for fraction in FRACTIONS:
        key = f"{fraction:g}"
        losing = [any(identity["on_ranked_page"] and not identity["survives"][key]
                      for identity in reading["identities"].values()) for reading in readings]
        at_risk[key] = {"trials": len(losing), "at_risk": sum(losing),
                        "any": any(losing), "all": bool(losing) and all(losing)}
    blind = [any(identity["on_ranked_page"] and not identity["survives_blind"]
                 for identity in reading["identities"].values()) for reading in readings]
    shares = [identity["best_share"] for reading in readings
              for identity in reading["identities"].values()
              if identity["best_share"] is not None]
    served = [any(identity["on_ranked_page"] for identity in reading["identities"].values())
              for reading in readings]
    return {
        "trials": len(readings),
        "trials_serving_gold_on_a_page": sum(served),
        "best_share_median": round(sorted(shares)[len(shares) // 2], 4) if shares else None,
        "worst_best_share": round(max(shares), 4) if shares else None,
        "at_risk": at_risk,
        "at_risk_blind": {"at_risk": sum(blind), "any": any(blind),
                          "all": bool(blind) and all(blind)},
    }


def suite_binding(run, questions):
    """Every manifest under this run must pin the suite it is being scored against.

    Scoring an archived run against the wrong question set produces a clean-looking table of
    nothing, and the manifests already carry the hash that rules it out.
    """
    digest = hashlib.sha256(questions.read_bytes()).hexdigest()
    # A run directory holds corpus copies, and a vendored index writes its own `manifest.json`
    # into one (`.zvec-grep/manifest.json`). A run manifest is the one that pins a question set,
    # so the field identifies it; anything without it is some other tool's bookkeeping.
    pinned = [(path, found) for path in sorted(run.rglob("manifest.json"))
              if (found := json.loads(path.read_text(encoding="utf-8")).get("questions_sha256"))]
    if not pinned:
        raise ValueError(f"no manifest.json under {run} pins a question set: cannot prove which "
                         f"suite this run used")
    wrong = [str(path) for path, found in pinned if found != digest]
    if wrong:
        raise ValueError(f"{questions} is not the question set these runs used: {', '.join(wrong)}")
    return digest


def report(run, questions_path, system):
    tasks = {task["id"]: task
             for task in json.loads(questions_path.read_text(encoding="utf-8"))}
    digest = suite_binding(run, questions_path)
    per_question, skipped = defaultdict(list), []
    for state_path in sorted(run.rglob("run.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("system") != system:
            continue
        if state.get("status") != "completed":
            skipped.append({"trial": str(state_path.parent), "reason": state.get("status")})
            continue
        task = tasks.get(state["task_id"])
        if task is None:
            raise ValueError(f"{state_path} names {state['task_id']}, absent from {questions_path}")
        reading = trial_reading(state_path.parent, task)
        if reading is None:
            skipped.append({"trial": str(state_path.parent), "reason": "no readable event stream"})
            continue
        per_question[state["task_id"]].append(reading)
    questions = {task_id: question_reading(readings)
                 for task_id, readings in sorted(per_question.items())}
    return {
        "version": "page-position-v1",
        "run": str(run),
        "system": system,
        "questions_file": str(questions_path),
        "questions_sha256": digest,
        "questions_read": len(questions),
        "trials_read": sum(len(readings) for readings in per_question.values()),
        "trials_skipped": skipped,
        "bands": bands(questions),
        "ceiling": ceiling(questions),
        "by_question": questions,
        "per_trial": {task_id: readings for task_id, readings in sorted(per_question.items())},
        "limitations":
            "Visibility is textual at row granularity - the generosity end_to_end.unretrieved "
            "already uses - so a row that only mentions the gold name counts as carrying it, and "
            "every figure here therefore understates what a cut removes. Only ranked pages are "
            "counted: an identity reached through read_source is reported as served off-page, not "
            "as at risk. Retrieval behaviour is held at what the archived run did, so this is the "
            "first-order ceiling on a degradation's cost, not its measured effect - a degraded arm "
            "asks different follow-up questions and some of them recover. --shuffle is absent by "
            "construction: it removes no row, so its ceiling on this reading is zero.",
    }


def bands(questions):
    """How many questions had their gold that far down the best page that served it."""
    counts = {f"top {band:g}": 0 for band in BANDS}
    counts["never on a ranked page"] = 0
    for reading in questions.values():
        share = reading["best_share_median"]
        if share is None:
            counts["never on a ranked page"] += 1
            continue
        for band in BANDS:
            if share <= band:
                counts[f"top {band:g}"] += 1
                break
    return counts


def ceiling(questions):
    """The headline: how many questions each knob setting could cost, at most."""
    result = {}
    for fraction in FRACTIONS:
        key = f"{fraction:g}"
        result[key] = {
            "questions_at_risk_any_repetition": sum(1 for reading in questions.values()
                                                    if reading["at_risk"][key]["any"]),
            "questions_at_risk_every_repetition": sum(1 for reading in questions.values()
                                                      if reading["at_risk"][key]["all"]),
        }
    result["blind_attribution"] = {
        "questions_at_risk_any_repetition": sum(1 for reading in questions.values()
                                                if reading["at_risk_blind"]["any"]),
        "questions_at_risk_every_repetition": sum(1 for reading in questions.values()
                                                  if reading["at_risk_blind"]["all"]),
    }
    return result


def render(result):
    lines = [f"{result['run']} [{result['system']}]",
             f"  {result['questions_read']} questions, {result['trials_read']} completed trials"
             f"; suite {Path(result['questions_file']).name}"]
    for entry in result["trials_skipped"]:
        lines.append(f"  skip {entry['trial']}: {entry['reason']}")
    lines.append("")
    lines.append("  where the gold sat on the best page that served it (median over repetitions)")
    for band, count in result["bands"].items():
        lines.append(f"    {band:<24} {count:>3}")
    lines.append("")
    lines.append("  ceiling: questions a knob could cost at most")
    lines.append(f"    {'setting':<24} {'any rep':>8} {'every rep':>10}")
    for key, row in result["ceiling"].items():
        label = key if key == "blind_attribution" else f"--page-fraction {key}"
        lines.append(f"    {label:<24} {row['questions_at_risk_any_repetition']:>8} "
                     f"{row['questions_at_risk_every_repetition']:>10}")
    lines.append("")
    lines.append(f"  {'question':<40} {'trials':>6} {'median':>7} {'worst':>7}  at risk by fraction")
    for task_id, reading in result["by_question"].items():
        flags = " ".join(f"{key}:{reading['at_risk'][key]['at_risk']}/{reading['trials']}"
                         for key in reading["at_risk"])
        median = f"{reading['best_share_median']:.2f}" if reading["best_share_median"] else "-"
        worst = f"{reading['worst_best_share']:.2f}" if reading["worst_best_share"] else "-"
        lines.append(f"  {task_id:<40} {reading['trials']:>6} {median:>7} {worst:>7}  {flags}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", type=Path, required=True, help="an archived run directory")
    parser.add_argument("--questions", type=Path, required=True,
                        help="the suite that run used; its sha256 is checked against the manifest")
    parser.add_argument("--system", default="retrieval-mcp", help="the arm whose pages to read")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args.run.resolve(strict=True), args.questions.resolve(strict=True),
                    args.system)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        benchmark.write_json(args.output, result)
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
