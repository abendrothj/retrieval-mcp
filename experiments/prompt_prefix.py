#!/usr/bin/env python3
"""What a condition costs before the first tool call, measured after the cache settles.

An arm's announcement is not free. The MCP arm's is delivered by the protocol; the CLI arm's is
a fragment in its prompt; the native arm has none. Before `runs/cli-transport-20260924` can read
a token difference as transport, it has to know what each arm paid just to be told its tools
exist.

The trap this instrument exists to avoid is the one that already cost this project a reading.
`runs/locbench-noshell-20260923` first measured `--disable shell_tool` by alternating the two
configurations and found it costing +9,600 tokens a request. Run consecutively the same flag
converges 15,555 -> 4,547 -> 451 -> 451, against 1,091 for the default: it is slightly *cheaper*.
The alternating figure was cache thrash, not a prefix. So conditions here are never interleaved:
each is run to convergence in its own consecutive block, and the converged value is the answer.
That is also why the prompt must require no tool call - a measurement of the prefix should not
include whatever the model decided to do about it.

Usage:
    python3 experiments/prompt_prefix.py --model gpt-5.6-luna \\
        --server target/release/retrieval-mcp \\
        --announcement runs/cli-transport-20260924/announcement.txt \\
        --repetitions 4 --output runs/cli-transport-20260924/prefix.json

Costs model calls. Trivial ones - four short turns per condition - but they are real requests.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WRAPPER = Path(__file__).with_name("codex_wrapper.py")
DEFAULT_TOOLS = ["search_exact", "read_source", "find_callers", "search_concept"]

# No tool can help with this, so the turn is the prefix plus a few tokens and nothing else.
TRIVIAL = ("Reply with exactly the word ok and nothing else. "
           "Do not call any tool, do not run any command, and do not read any file.\n")


def one_run(model, workdir, mcp, prompt, label, index):
    """One Codex turn under the harness's own wrapper, so isolation matches a real trial."""
    run_dir = workdir / f"{label}-{index}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    cwd = workdir / "empty"
    cwd.mkdir(exist_ok=True)
    environment = dict(os.environ)
    # One shared credential home: a subscription refresh token is single-use, so per-run copies
    # invalidate each other. The wrapper documents this; it is set here so a caller cannot forget.
    environment.setdefault("CODEX_CREDENTIAL_HOME", str(workdir / "credential-home"))
    result = subprocess.run(
        [sys.executable, str(WRAPPER), model, str(run_dir / "mcp.json"),
         str(run_dir / "prompt.txt"), str(run_dir)],
        cwd=cwd, env=environment, capture_output=True, text=True, timeout=600,
    )
    usage, attached = None, []
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("type") == "result" and message.get("usage"):
            usage = message["usage"]
        if message.get("subtype") == "init":
            attached = message.get("tools") or []
    if usage is None:
        raise SystemExit(f"{label} run {index} produced no usage:\n{result.stderr[-1500:]}")
    listed = run_dir / "tools.json"
    if listed.is_file():
        try:
            names = json.loads(listed.read_text(encoding="utf-8"))
        except ValueError:
            names = []
        if isinstance(names, dict):
            names = names.get("tools") or []
        attached = [n.get("name", n) if isinstance(n, dict) else n for n in names]
    # A condition that was meant to carry an MCP server and did not is indistinguishable from
    # the native condition, and would be reported as "attaching the tools costs nothing" when
    # what happened is that nothing was attached. The wrapper announces what connected, so this
    # is checked rather than assumed.
    return usage, attached


def block(model, workdir, label, mcp, prompt, repetitions):
    """One condition, run consecutively to convergence. Never interleaved with another."""
    runs, tools = [], []
    expected = bool(mcp.get("mcpServers") or {})
    for index in range(repetitions):
        usage, tools = one_run(model, workdir, mcp, prompt, label, index)
        if expected and not tools:
            raise SystemExit(
                f"{label} declared an MCP server and none connected; the condition would be "
                "indistinguishable from native and would read as 'the tools cost nothing'")
        total = usage.get("input_tokens") or 0
        runs.append({
            "input_tokens": total,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
        })
        print(f"  {label:8} run {index}: input {total:>8,}  "
              f"cache_read {runs[-1]['cache_read']:>8,}  tools {len(tools)}", flush=True)
    return runs, tools


def converged(runs):
    """The last two runs, if they agree within 2%, else None: a block that never settled has
    not measured a prefix and must not be reported as though it had."""
    if len(runs) < 2:
        return None
    last, previous = runs[-1]["input_tokens"], runs[-2]["input_tokens"]
    if last == 0:
        return None
    return last if abs(last - previous) <= max(1, 0.02 * last) else None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True)
    parser.add_argument("--server", type=Path, required=True,
                        help="the retrieval-mcp binary, for the MCP condition")
    parser.add_argument("--announcement", type=Path, required=True,
                        help="the CLI arm's prompt fragment")
    parser.add_argument("--repetitions", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not args.server.is_file():
        raise SystemExit(f"not a binary: {args.server}")
    announcement = args.announcement.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        empty = workdir / "empty"
        empty.mkdir()
        gate_config = workdir / "gate.json"
        gate_config.write_text(json.dumps({
            "upstreams": [{
                "id": "retrieval",
                "command": [str(args.server.resolve()), "--root", str(empty),
                            "--semantic-command", '["/usr/bin/true"]',
                            "--timeout-seconds", "120"],
                "environment": {},
                "visible_tools": DEFAULT_TOOLS,
                "expected_upstream_tools": DEFAULT_TOOLS,
            }],
            "root": str(empty),
            "max_calls": 30,
            "max_bytes": 400000,
            "timeout": 120,
            "tools_path": str(workdir / "tools.json"),
        }), encoding="utf-8")
        gate = Path(__file__).with_name("comparison_gate.py")
        conditions = [
            ("native", {"mcpServers": {}}, TRIVIAL),
            ("mcp", {"mcpServers": {"retrieval": {
                "command": sys.executable,
                "args": [str(gate), "--config", str(gate_config)]}}}, TRIVIAL),
            ("cli", {"mcpServers": {}}, announcement + "\n" + TRIVIAL),
        ]
        report = {"model": args.model, "repetitions": args.repetitions,
                  "announcement_chars": len(announcement), "conditions": {}}
        for label, mcp, prompt in conditions:
            print(f"{label}: {args.repetitions} consecutive runs", flush=True)
            runs, tools = block(args.model, workdir, label, mcp, prompt, args.repetitions)
            report["conditions"][label] = {
                "runs": runs,
                "tools_attached": tools,
                "converged_input_tokens": converged(runs),
            }

    native = report["conditions"]["native"]["converged_input_tokens"]
    for label in ("mcp", "cli"):
        settled = report["conditions"][label]["converged_input_tokens"]
        if native and settled:
            report["conditions"][label]["announcement_cost_vs_native"] = settled - native
    json.dump(report, sys.stdout, indent=2)
    print()
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
