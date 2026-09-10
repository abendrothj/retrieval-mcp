#!/usr/bin/env python3
"""Audit the questions no arm ever solved. No model calls, no reruns.

A question that every system fails is either a hard question or a broken one, and the two look
identical in a score table. This separates them mechanically:

  * `shape` - the gold is a list or object and the answer is prose, so the frozen grader scores
    zero however right the answer is. Reported as `names_all_gold` when the prose does name every
    gold symbol.
  * `gold` - the gold's own claim disagrees with an independent ripgrep enumeration of the corpus,
    so the question cannot be scored until the gold is repaired.
  * `unqualified` - the answer names the gold symbol but not its file, and the name is defined more
    than once, so no resolver can accept it.
  * `wrong` - the answer names something else entirely.

Only the last category is evidence about retrieval or reasoning. Everything else is evidence about
the benchmark.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

import quality_pass

NAME = re.compile(r"[A-Za-z_$][\w$]*")


def gold_symbols(answer):
    """Every (path, name) pair a typed gold answer asserts, whatever its shape."""
    found = []

    def walk(value):
        if isinstance(value, str) and "::" in value:
            path, _, rest = value.partition("::")
            found.append((path, rest.split("::")[-1]))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(answer)
    return found


def call_sites(corpus, name):
    """Independent enumeration of a helper's call sites, by ripgrep over the corpus."""
    found = subprocess.run(
        ["rg", "--no-config", "-n", "--no-heading", rf"\b{re.escape(name)}\s*\(",
         "-g", "*.ts", "-g", "*.tsx", "."],
        cwd=corpus, capture_output=True, text=True, timeout=120)
    sites = []
    for line in found.stdout.splitlines():
        path, _, rest = line.partition(":")
        path = path.lstrip("./")
        # A definition line is not a call site.
        if re.search(rf"(function|const|let|class)\s+{re.escape(name)}\b", rest):
            continue
        sites.append(path)
    return sites


def enclosing(path, line):
    """The definition a line sits inside, by reading backwards. Independent of every index.

    Python is indentation-scoped, so the enclosing definition is the nearest `def` or `class`
    indented less than the call site; brace languages are matched on their declaration syntax.
    """
    source = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if path.suffix == ".py":
        call = source[line - 1] if line <= len(source) else ""
        depth = len(call) - len(call.lstrip())
        for position in range(min(line, len(source)) - 1, -1, -1):
            text = source[position]
            declaration = re.match(r"(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", text)
            if declaration and len(declaration.group(1)) < depth:
                return declaration.group(2)
        return None
    for position in range(min(line, len(source)) - 1, -1, -1):
        text = source[position]
        method = re.match(
            r"\s*(?:public |private |protected |static |override |async )*"
            r"([A-Za-z_$][\w$]*)\s*\(.*\)\s*[:{]", text)
        if method and method.group(1) not in ("if", "for", "while", "switch", "catch", "return"):
            return method.group(1)
        function = re.match(r"\s*(?:export )?(?:async )?function\s+([A-Za-z_$][\w$]*)", text)
        if function:
            return function.group(1)
    return None


def true_callers(corpus, name, defining_path):
    """Every enclosing definition that calls `name` outside its own module, from source alone."""
    found = subprocess.run(
        ["rg", "--no-config", "-n", "--no-heading", rf"\b{re.escape(name)}\s*\(",
         "-g", "*.ts", "-g", "*.tsx", "-g", "*.py", "-g", "*.rs", "."],
        cwd=corpus, capture_output=True, text=True, timeout=120)
    callers = set()
    for line in found.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, body = rest.partition(":")
        path = path.lstrip("./")
        if path == defining_path or not number.isdigit():
            continue
        # A declaration is not a call site, in any of the three languages.
        if re.search(rf"(function|const|let|class|def|fn)\s+{re.escape(name)}\b", body):
            continue
        owner = enclosing(Path(corpus) / path, int(number))
        if owner:
            callers.add(f"{path}::{owner}")
    return sorted(callers)


def audit(run, questions, corpus, helpers):
    tasks = {task["id"]: task for task in json.loads(questions.read_text(encoding="utf-8"))}
    index = quality_pass.definitions(corpus)
    trials = []
    for path in sorted(run.glob("trial-*/run.json")):
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["status"] == "provider_error":
            continue
        trials.append(state)
    by_task = {}
    for state in trials:
        by_task.setdefault(state["task_id"], []).append(state)

    findings = {}
    for task_id, states in sorted(by_task.items()):
        if any(state.get("resolved_correct") for state in states):
            continue
        task = tasks[task_id]
        gold = task["expected_json"]["answer"]
        pairs = gold_symbols(gold)
        rows = []
        for state in states:
            written = quality_pass.answer_json(state.get("answer"))
            text = written if isinstance(written, str) else json.dumps(written)
            named = {token for token in NAME.findall(text or "")}
            missing = sorted({name for _, name in pairs} - named)
            shape = type(gold).__name__ if not isinstance(written, type(gold)) else None
            ambiguous = [name for _, name in pairs
                         if name in named and len(index.get(name, ())) > 1
                         and not any(path in (text or "") for path, other in pairs if other == name)]
            rows.append({
                "system": state["system"],
                "repetition": state["repetition"],
                "answer": text or "",
                "names_all_gold": not missing,
                "missing_gold_names": missing,
                "shape_mismatch": shape,
                "unqualified_namesakes": ambiguous,
                "verdict": ("wrong" if missing else
                            "unqualified" if ambiguous else
                            "shape" if shape else "other"),
            })
        # A caller question's gold is checked against the corpus rather than taken on trust, but
        # only where the helper being called is unambiguous: an object gold names it, or the
        # operator names it with --helper. Guessing the helper from a list gold reads a common
        # name like `initialize` as the target and produces nonsense.
        gold_claims = ["::".join(pair) for pair in pairs]
        helper = helpers.get(task_id) or (
            gold.get("implementation") if isinstance(gold, dict) else None)
        verified, gold_defect = None, None
        if helper and "::" in helper:
            helper_path, helper_name = helper.split("::", 1)
            verified = true_callers(corpus, helper_name.split("::")[-1], helper_path)
            claimed = {claim for claim in gold_claims if claim != helper}
            if verified and claimed != set(verified):
                gold_defect = {
                    "helper": helper,
                    "claimed_callers": sorted(claimed),
                    "verified_callers": verified,
                    "claimed_but_not_found": sorted(claimed - set(verified)),
                    "found_but_not_claimed": sorted(set(verified) - claimed),
                }
        findings[task_id] = {
            "category": task["category"],
            "question": task["question"],
            "gold": gold,
            "gold_shape": type(gold).__name__,
            "gold_symbols": gold_claims,
            "gold_defect": gold_defect,
            "trials": rows,
            "verdicts": dict(Counter(row["verdict"] for row in rows)),
        }
    return {
        "version": "audit-failures-v1",
        "run": str(run),
        "questions_total": len(by_task),
        "questions_never_solved": len(findings),
        "trials_audited": sum(len(f["trials"]) for f in findings.values()),
        "verdicts": dict(Counter(row["verdict"] for f in findings.values() for row in f["trials"])),
        "findings": findings,
        "limitations": "Mechanical checks only: name presence, answer shape, namesake ambiguity, "
                       "and an independent call-site count. Whether a question is genuinely "
                       "ambiguous still requires reading the corpus.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", action="append", default=[],
                        help="task_id=path::symbol; the helper a caller question is about, when "
                             "the gold does not name it")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    helpers = dict(entry.split("=", 1) for entry in args.helper)
    result = audit(args.run.resolve(strict=True), args.questions, args.corpus.resolve(strict=True),
                   helpers)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("questions_total", "questions_never_solved", "trials_audited", "verdicts")},
                     indent=2))
    for task_id, finding in result["findings"].items():
        print(f"{task_id:38s} {finding['gold_shape']:5s} {finding['verdicts']}")
        if finding["gold_defect"]:
            defect = finding["gold_defect"]
            print(f"    gold defect: {len(defect['claimed_but_not_found'])} claimed callers absent, "
                  f"{len(defect['found_but_not_claimed'])} real callers unclaimed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
