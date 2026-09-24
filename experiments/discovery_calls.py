#!/usr/bin/env python3
"""How many round trips an arm spends searching for its own tools, from a rollout. No model calls.

On codex-cli every arm drives one tool, `exec`, and writes code in it. So an MCP arm looking for
its own tools does not call anything named `tool_search` - it writes JavaScript that filters the
catalogue, `ALL_TOOLS.filter(x => /retriev|codebase|symbol|definition/i.test(x.name))`, and the
call reaching the retrieval server never happens. That is why `calls.jsonl` is blind to it: the
gate log sees only what reaches the server, and `command_execution` sees only shell.

This counts distinct `exec` scripts whose source mentions `ALL_TOOLS`, per trial, per arm. Two
properties are deliberate:

  * DISTINCT scripts, not occurrences. The same script is echoed in both `codex-events.jsonl` and
    `codex-session.jsonl`, so counting occurrences double-counts - the error that turned one path
    error into two in a sibling analysis.
  * The `input` field of an exec payload, not any mention anywhere. `tool_search` appears in
    system-prompt text on this client without ever being called, so a text match over the whole
    rollout reports discovery that did not happen, and a match on the tool name alone reports none
    that did.

It reproduces the figures published from an uncommitted pass: 2.40 scripts a trial in 21 of 25
bare-MCP trials of runs/cli-locbench-20260924, halved to 1.00 in 25 of 25 when a skill document
naming the four tools sits beside the server. The document's measured mechanism is that an agent
which has read it does not need to go looking.

    discovery_calls.py --run runs/cli-locbench-20260924/report
"""
import argparse
import json
from pathlib import Path

MARKER = "ALL_TOOLS"


def discovery_scripts(trial):
    """Distinct exec scripts in one trial that query the tool catalogue."""
    scripts = set()
    session = trial / "codex-session.jsonl"
    if not session.is_file():
        return scripts
    for line in session.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = (json.loads(line).get("payload") or {})
        except ValueError:
            continue
        source = payload.get("input")
        if isinstance(source, str) and MARKER in source:
            scripts.add(source)
    return scripts


def report(run):
    arms = {}
    for state_path in sorted(Path(run).glob("trial-*/run.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") != "completed":
            continue
        counts = arms.setdefault(state["system"], [])
        counts.append(len(discovery_scripts(state_path.parent)))
    return {"version": "discovery-calls-v1", "run": str(run),
            "arms": {arm: {"trials": len(counts),
                           "discovery_calls_per_trial": round(sum(counts) / len(counts), 2),
                           "trials_with_discovery": sum(1 for c in counts if c),
                           "total": sum(counts)}
                     for arm, counts in sorted(arms.items()) if counts}}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", type=Path, required=True, help="an archived run's report directory")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args.run)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
