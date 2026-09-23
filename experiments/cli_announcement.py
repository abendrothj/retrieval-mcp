#!/usr/bin/env python3
"""Generate the CLI arm's prompt announcement from the binaries, and price it against MCP's.

`runs/cli-transport-20260924` compares the four retrieval operations over two channels. The MCP
arm is announced by the protocol: `initialize` returns routing instructions and `tools/list`
returns a description per tool, and each tool's input schema states its arguments: 7,958
characters the model receives without asking. A command is announced by nothing at all, so the
arm needs a prompt fragment in its place - and that fragment is
the single most dangerous thing in the study, because an operator-written routing document that
reached 2,109 trials is what this project spent 2026-09-23 auditing.

Three rules follow, and this script exists to enforce them mechanically rather than by good
intentions.

Generated, never authored. Every sentence comes from the server's own `initialize` instructions,
its own tool descriptions, or the CLI's own `--help`. Nobody writes persuasive prose into the
arm, because nobody writes the arm.

Matched, not minimised. `--help` alone is 910 characters against MCP's 7,958, so an arm given
only the synopsis would be an arm given a ninth of the guidance - transport confounded with how
much advice each side got. The routing text and the per-operation descriptions carry over intact,
with tool names rewritten as the subcommands that do the same thing.

Priced and pinned. The script reports both sides' size and writes a sha256, so the registration
records what reached the model instead of asserting that something did.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# The same operation, named for each channel. Order is the order a route mentions them.
SUBCOMMANDS = {
    "search_exact": "retrieval search",
    "search_concept": "retrieval concept",
    "find_callers": "retrieval callers",
    "read_source": "retrieval read",
}


def rename(text):
    """Tool names become the subcommand that runs the same code. Longest first, so that
    `search_concept` is not first rewritten by the `search_exact` rule's shared prefix."""
    for tool in sorted(SUBCOMMANDS, key=len, reverse=True):
        text = text.replace(tool, SUBCOMMANDS[tool])
    return text


def handshake(server, root):
    """The two things the protocol tells the model for free: instructions, and descriptions."""
    proc = subprocess.Popen(
        [str(server), "--root", str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def reply(wanted):
        while True:
            line = proc.stdout.readline()
            if not line:
                raise SystemExit("the server closed before answering")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == wanted:
                return message

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "announcement", "version": "0"}}})
    initialised = reply(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    listed = reply(2)
    # Both pipes, not just stdin: the suite runs with -W error::ResourceWarning and an
    # unclosed stdout is exactly the kind of leak that turns into a flake under discovery.
    with proc:
        proc.stdin.close()
        proc.wait(timeout=30)
    return (initialised["result"].get("instructions") or "",
            listed["result"]["tools"])


def announcement(server, cli):
    """The fragment the harness puts in the CLI arm's prompt, and the MCP side's size."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sample.rs").write_text("fn needle() {}\n")
        instructions, tools = handshake(server, root)
    help_text = subprocess.run([str(cli), "--help"], capture_output=True, text=True,
                               check=True).stdout
    descriptions = "\n".join(
        f"{SUBCOMMANDS.get(tool['name'], tool['name'])}: {tool.get('description', '')}"
        for tool in tools
    )
    # The input schemas count. They are how the MCP arm learns each argument's name, type and
    # range, and `--help` is the only thing telling the CLI arm the same - so leaving them out
    # priced the two announcements on different terms and made the CLI look 25% more expensive
    # than MCP when it is in fact about three quarters of it.
    schemas = "".join(
        json.dumps(tool.get("inputSchema") or {}, separators=(",", ":")) for tool in tools
    )
    parts = {
        "instructions": len(instructions),
        "descriptions": len(descriptions),
        "input_schemas": len(schemas),
    }
    mcp_side = instructions + descriptions + schemas

    text = "\n\n".join([
        "A command `retrieval` is available on PATH. It searches and reads this repository "
        "from an index it builds per invocation. Its rows go to stdout and its coverage "
        "reporting to stderr, so its output composes with the usual shell tools.",
        help_text.strip(),
        rename(instructions).strip(),
        rename(descriptions).strip(),
    ]) + "\n"
    return text, mcp_side, help_text, tools, parts


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", type=Path, required=True, help="the retrieval-mcp binary")
    parser.add_argument("--cli", type=Path, required=True, help="the retrieval binary")
    parser.add_argument("--output", type=Path, help="write the announcement here")
    args = parser.parse_args()

    for binary in (args.server, args.cli):
        if not binary.is_file():
            raise SystemExit(f"not a binary: {binary}")

    text, mcp_side, help_text, tools, parts = announcement(args.server, args.cli)
    report = {
        "tools": [tool["name"] for tool in tools],
        "mcp_announcement_chars": len(mcp_side),
        "mcp_parts": parts,
        "cli_announcement_chars": len(text),
        "help_only_chars": len(help_text),
        "ratio_cli_over_mcp": round(len(text) / len(mcp_side), 3),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "note": "Characters, not tokens. The registration requires the token figure to be "
                "measured on this client with the cache warm, consecutive runs, because "
                "interleaving two configurations measures the cache and not the prefix.",
    }
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        report["written"] = str(args.output)
    json.dump(report, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
