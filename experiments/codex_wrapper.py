#!/usr/bin/env python3
"""Drive Codex CLI non-interactively for one comparison trial and translate its event stream.

Codex 0.153 emits `thread.started`, `turn.started`, `item.started`, `item.completed`,
`turn.completed`, and `error` JSONL events. This wrapper owns per-trial isolation (its own
CODEX_HOME, no user config, ephemeral sessions, read-only sandbox), MCP wiring, and translation
into the harness transcript shape.

Codex reports usage once per turn rather than once per step, so a per-step token profile is not
recoverable from this client. The analyzer measures calls and bytes after the first hit instead
and reports token totals only; see end_to_end.py.
"""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

from opencode_wrapper import load_mcp_retrieval, load_retrieval_tools, provider_failure

# Codex's own tools. Shell is the native codebase surface; the rest are contamination here.
NATIVE_ITEMS = {"command_execution", "reasoning", "agent_message", "todo_list"}
CONTAMINATION_ITEMS = {"file_change", "web_search", "patch_apply"}


# Interpreters and their libraries are machinery, not evidence; a corpus path is evidence. Only
# the second kind decides whether a trial read something the other arm could not.
TOOLING_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/local/bin/",
                    "/opt/homebrew/bin/", "/dev/null", "/dev/stderr", "/dev/stdout")


def absolute_paths(command):
    """Absolute path arguments in a shell command.

    Anchored at a token boundary, so the slash inside the relative `fs/lockd` starts nothing, and
    the first character after the slash must be a name character, so the `/` alternation inside a
    quoted regex is not a path. `|` and `:` are deliberately not boundaries: inside `'(^|/)(pids)'`
    the bare slash is regex syntax.
    """
    return re.findall(r"""(?:^|[\s"'=<>(])(/[A-Za-z0-9._][^\s"'|;)]*)""", command)


def outside_corpus(token, root):
    """Is this absolute path token evidence from somewhere other than the corpus copy?

    Existence is the second half of the test, and it is what separates a path from a pattern:
    `rg '/gfs2/' fs` and `rg '(^|/)(pids|cgroup)'` both look like absolute paths to a tokenizer and
    neither names a file on this machine. Flagging them cost two clean native trials their
    eligibility before anyone checked what the flag meant.
    """
    if token.startswith(TOOLING_PREFIXES):
        return False
    resolved = os.path.realpath(token)
    if resolved == root or resolved.startswith(root.rstrip("/") + "/"):
        return False
    return os.path.lexists(resolved)


# `"agents_md":{}` is the empty config key every recent rollout writes; anything else is content.
# Stated as what content looks like - a non-empty object, string or list - rather than as "not
# empty", because `\s*` backtracks out of a negative lookahead and lets `"agents_md": {}` through.
CARRIED_AGENTS_MD = re.compile(r'"agents_md"\s*:\s*(?:\{\s*"|"[^"]|\[\s*[^\s\]])')


def session_instructions(session):
    """Did the client put a project document into this trial's context after all?

    The flag above stops Codex 0.155 from doing it. A client update can start again, and the
    failure is silent: the document arrives in the first user message, not through a tool, so the
    shell-command detector cannot see it. The rollout log records what was sent, so it is read
    back. Five kernel studies were measured with 3,500-4,900 tokens of this project's own
    `AGENTS.md` in both arms' contexts because nothing looked here.

    Match the key only when it carries something. Newer Codex rollouts write `"agents_md":{}`
    into the session config on every run, clean or not, so a bare substring test reported all 226
    trials of the three LOC-BENCH studies as carrying a document they did not carry. A detector
    that fires on every clean run is worse than none: it is the flag nobody can act on.
    """
    if not session.is_file():
        return False
    for line in session.read_text(encoding="utf-8", errors="replace").splitlines():
        if "AGENTS.md instructions for" in line or CARRIED_AGENTS_MD.search(line):
            return True
    return False


def parse_events(events, corpus=None):
    """Translate Codex's stream, and refuse to be quiet about evidence from outside the corpus.

    A shell command naming an absolute path that is not inside the corpus is contamination in the
    same sense a web search is: the trial read something no other arm's corpus contains. It was a
    skill document describing this server's own tools, and it went unnoticed for three published
    studies because nothing looked.
    """
    final, usage, client_error, provider_detail = None, None, None, None
    tool_calls, unexpected = [], set()
    root = os.path.realpath(corpus) if corpus else None
    for event in events:
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "item.completed":
            if item.get("type") == "agent_message":
                final = item.get("text")
            elif item.get("type") == "command_execution":
                command = item.get("command") or ""
                if root and any(outside_corpus(token, root)
                                for token in absolute_paths(str(command))):
                    unexpected.add("read_outside_corpus")
                tool_calls.append({"name": "shell", "command": command,
                                   "output": item.get("aggregated_output") or ""})
            elif item.get("type") == "mcp_tool_call":
                server = item.get("server")
                if server not in (None, "retrieval"):
                    unexpected.add("other_mcp_server")
                tool_calls.append({"name": item.get("tool") or item.get("name") or "mcp",
                                   "output": json.dumps(item.get("result") or item.get("output") or "")})
            elif item.get("type") in CONTAMINATION_ITEMS:
                unexpected.add(item["type"])
        elif kind == "turn.completed":
            counts = event.get("usage") or {}
            usage = {
                "input_tokens": int(counts.get("input_tokens") or 0),
                "output_tokens": int(counts.get("output_tokens") or 0),
                "reasoning_tokens": int(counts.get("reasoning_output_tokens") or 0),
                "cache_read_input_tokens": int(counts.get("cached_input_tokens") or 0),
                "cache_creation_input_tokens": int(counts.get("cache_write_input_tokens") or 0),
                # A subscription seat has no per-call price; tokens are the currency here.
                "cost_usd": 0.0,
            }
        elif kind in ("error", "turn.failed"):
            error = event.get("error") or (event.get("turn") or {}).get("error") or {}
            if isinstance(error, str):
                error = {"name": "client_error", "data": {"message": error}}
            provider = provider_failure(error)
            if provider:
                client_error, provider_detail = provider
            else:
                client_error = error.get("name", "client_error") if isinstance(error, dict) else "client_error"
    return {"final": final, "usage": usage, "client_error": client_error,
            "provider_detail": provider_detail, "tool_calls": tool_calls,
            "unexpected_tools": sorted(unexpected)}


def main():
    model, mcp_config, prompt_file, run_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    run_dir = Path(run_dir)
    prompt = Path(prompt_file).read_text(encoding="utf-8")
    retrieval = load_mcp_retrieval(mcp_config)
    retrieval_tools = load_retrieval_tools(mcp_config)

    # Isolation, and one shared credential.
    #
    # `--ignore-user-config` covers Codex's own config and does not cover the operator's home
    # directory. Every trial of the first Linux launch opened
    # `~/.agents/skills/retrieval-mcp/SKILL.md` as its first command - a routing guide for the very
    # tools under test, written by this project, read by both arms from outside the corpus. So the
    # trial gets its own HOME: a shell started in it finds no profile, no skills directory and no
    # agent instructions, and the corpus copy is the only evidence in the session.
    #
    # The credential cannot be per-trial. A subscription refresh token is single-use, so copying
    # `auth.json` into 126 trial homes means the first refresh invalidates every other copy: the
    # second launch died at trial 41 with "your refresh token was already used". One shared
    # credential home, seeded once and written back to by whichever trial refreshes, keeps the
    # chain coherent - trials are serial, so there is no race - while HOME stays trial-local.
    home = Path(os.environ.get("CODEX_CREDENTIAL_HOME") or run_dir / "codex-home")
    home.mkdir(parents=True, exist_ok=True)
    credential = Path(os.environ.get("CODEX_AUTH_SOURCE", Path.home() / ".codex" / "auth.json"))
    if credential.is_file() and not (home / "auth.json").is_file():
        shutil.copy2(credential, home / "auth.json")
    session_home = run_dir / "codex-home"
    session_home.mkdir(parents=True, exist_ok=True)

    # Per-request accounting, for both arms alike. Codex reports usage once per turn on stdout,
    # which cannot say whether a session was expensive because it took many model requests or
    # because each carried more context - the question three kernel studies could not answer. A
    # non-ephemeral session writes a rollout log with a `token_count` event per request, so the
    # run keeps one and the analysis reads it. Nothing else about the session changes.
    #
    # And no project document. `--cd` points at a corpus copy under `runs/`, which is inside this
    # repository's own git tree, so Codex walked up to the repository root and put this project's
    # `AGENTS.md` - its claim, its arms, its expected direction, and from 2026-09-20 the symbol one
    # kernel caller question describes - into the first user message of every trial, both arms
    # alike. `--ignore-user-config` does not cover it; `project_doc_max_bytes=0` does. Measured
    # against a request sink on 2026-09-22: 73,338 bytes of request body without the flag, 53,384
    # with, and the `# AGENTS.md instructions for ...` block is the whole difference.
    before = set(home.glob("sessions/**/*.jsonl"))
    command = ["codex", "exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
               "--sandbox", "read-only", "--cd", os.getcwd(), "--model", model,
               "-c", "project_doc_max_bytes=0"]
    # An MCP arm on this client keeps its shell unless it is taken away, so every Codex
    # comparison in this record measures shell-plus-MCP against shell rather than MCP against
    # shell. `shell_tool` is a stable feature flag and disabling it removes the tool: a session
    # asked to run a command executes none and reports having no such tool. It is not free - the
    # prompt prefix changes - so the arm that uses it has its prefix measured and recorded.
    if os.environ.get("CODEX_DISABLE_SHELL") == "1":
        command += ["--disable", "shell_tool"]
    if retrieval and retrieval.get("command"):
        command += ["-c", f"mcp_servers.retrieval.command={json.dumps(retrieval['command'])}",
                    "-c", "mcp_servers.retrieval.args=" + json.dumps(retrieval.get("args", []))]
    command.append(prompt)

    isolated = dict(os.environ, CODEX_HOME=str(home), HOME=str(session_home),
                    XDG_CONFIG_HOME=str(session_home / "config"),
                    XDG_DATA_HOME=str(session_home / "data"))
    process = subprocess.run(command, env=isolated, capture_output=True, text=True, timeout=None)
    (run_dir / "codex-events.jsonl").write_text(process.stdout, encoding="utf-8")
    (run_dir / "codex-stderr.log").write_text(process.stderr, encoding="utf-8")
    # The session log belongs to this trial, not to the shared credential home it landed in.
    for rollout in sorted(set(home.glob("sessions/**/*.jsonl")) - before):
        shutil.move(str(rollout), run_dir / "codex-session.jsonl")
    requests = []
    session = run_dir / "codex-session.jsonl"
    if session.is_file():
        for line in session.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                payload = (json.loads(line).get("payload") or {})
            except ValueError:
                continue
            if payload.get("type") == "token_count":
                last = (payload.get("info") or {}).get("last_token_usage") or {}
                if last.get("input_tokens"):
                    requests.append(last["input_tokens"])
    (run_dir / "requests.json").write_text(json.dumps(requests), encoding="utf-8")

    events = []
    for line in process.stdout.splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    outcome = parse_events(events, os.getcwd())
    if session_instructions(session):
        outcome["unexpected_tools"] = sorted(set(outcome["unexpected_tools"]) | {"project_doc"})

    tools = sorted({"mcp__retrieval__" + tool for tool in retrieval_tools})
    if retrieval:
        print(json.dumps({"type": "system", "subtype": "init", "tools": tools,
                          "mcp_servers": [{"name": "retrieval", "status": "connected"}]}))
    for name in outcome["unexpected_tools"]:
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "codex", "name": name, "input": {}}]}}))
    print(json.dumps({
        "type": "result",
        "is_error": outcome["client_error"] is not None or process.returncode != 0,
        "subtype": outcome["client_error"] or ("client_error" if process.returncode != 0 else None),
        "provider_detail": outcome["provider_detail"],
        "result": outcome["final"],
        "usage": outcome["usage"] or {},
    }))


if __name__ == "__main__":
    main()
