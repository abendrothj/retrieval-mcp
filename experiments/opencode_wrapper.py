#!/usr/bin/env python3
"""Drive OpenCode non-interactively for one comparison trial and translate its event stream.

OpenCode 1.18 emits `step_start`, `tool_use`, `text`, `step_finish`, and `error` JSON events.
This wrapper owns: per-trial config isolation (no global MCP servers leak in), model/variant
pinning, native-tool classification, and translation into the harness's recognized transcript
shape so `benchmark.transcript_outcome` and the analyzers work unchanged.

The retrieval MCP calls themselves are already recorded by the comparison gate in calls.jsonl;
this wrapper only needs to surface the final answer, token usage, and contamination.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

# Native codebase tools allowed in the "real environment". Anything else the model reaches for
# (web, webfetch, edit, write, patch, a second MCP server) is contamination and excludes the trial.
NATIVE_TOOLS = {"read", "grep", "glob", "list", "bash", "lsp"}
NATIVE_PREFIXES = ("todo",)


def load_mcp_retrieval(mcp_path):
    try:
        document = json.loads(Path(mcp_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document.get("mcpServers", {}).get("retrieval")


def load_retrieval_tools(mcp_path):
    """The comparison gate's visible tool names, read from the gate config it was launched with."""
    retrieval = load_mcp_retrieval(mcp_path)
    if not retrieval:
        return set()
    args = retrieval.get("args", [])
    if "--config" not in args:
        return set()
    gate_path = args[args.index("--config") + 1]
    try:
        gate = json.loads(Path(gate_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(gate.get("visible_tools", []))


def classify_tool(name, retrieval_tools):
    if name in retrieval_tools or any(name.endswith("_" + tool) for tool in retrieval_tools):
        return "retrieval"
    if name in NATIVE_TOOLS or name.startswith(NATIVE_PREFIXES):
        return "native"
    return "contamination"


def parse_events(events, retrieval_tools):
    """Reduce an OpenCode `run --format json` stream to the harness-relevant outcome."""
    final = None
    usage = None
    client_error = None
    tool_calls = []
    unexpected = set()
    text_by_message = {}
    message_order = []
    for event in events:
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "step_start":
            message_order.append(part.get("messageID"))
        elif kind == "step_finish":
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            if usage is None:
                usage = {
                    "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                    "cost_usd": 0.0,
                }
            usage["input_tokens"] += int(tokens.get("input") or 0)
            usage["output_tokens"] += int(tokens.get("output") or 0)
            usage["reasoning_tokens"] += int(tokens.get("reasoning") or 0)
            usage["cache_read_input_tokens"] += int(cache.get("read") or 0)
            usage["cache_creation_input_tokens"] += int(cache.get("write") or 0)
            usage["cost_usd"] += float(part.get("cost") or 0.0)
        elif kind == "error":
            error = event.get("error") or {}
            client_error = error.get("name", "client_error") if isinstance(error, dict) else "client_error"
        elif kind == "text":
            message_id = part.get("messageID")
            text_by_message.setdefault(message_id, []).append(part.get("text") or "")
        elif kind == "tool_use":
            name = part.get("tool") or ""
            state = part.get("state") or {}
            tool_calls.append({"name": name, "callID": part.get("callID"), "input": state.get("input")})
            if classify_tool(name, retrieval_tools) == "contamination":
                unexpected.add(name)
    if message_order:
        for message_id in reversed(message_order):
            if message_id in text_by_message and text_by_message[message_id]:
                final = "".join(text_by_message[message_id])
                break
    return {
        "final": final,
        "usage": usage,
        "client_error": client_error,
        "tool_calls": tool_calls,
        "unexpected_tools": sorted(unexpected),
    }


def main():
    model, mcp_config, prompt_file, run_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    run_dir = Path(run_dir)
    prompt = Path(prompt_file).read_text(encoding="utf-8")
    retrieval = load_mcp_retrieval(mcp_config)
    retrieval_tools = load_retrieval_tools(mcp_config)

    config = {}
    if retrieval and retrieval.get("command"):
        config["mcp"] = {
            "retrieval": {
                "enabled": True,
                "type": "local",
                "command": [retrieval["command"], *retrieval.get("args", [])],
            }
        }
    permissions = {
        "*": "deny",
        "read": "allow", "grep": "allow", "glob": "allow", "list": "allow",
        "bash": "allow", "lsp": "allow",
    }
    permissions.update({f"retrieval_{tool}": "allow" for tool in retrieval_tools})
    config["permission"] = permissions

    config_dir = run_dir / "opencode-home"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    command = ["opencode", "run", "--format", "json", "--pure", "--model", model]
    variant = os.environ.get("COMPARISON_OPENCODE_VARIANT")
    if variant:
        command += ["--variant", variant]
    command += ["--dir", os.getcwd(), prompt]
    env = dict(
        os.environ,
        OPENCODE_CONFIG_DIR=str(config_dir),
        XDG_CONFIG_HOME=str(run_dir / "xdg-config"),
        XDG_DATA_HOME=str(run_dir / "xdg-data"),
        XDG_CACHE_HOME=str(run_dir / "xdg-cache"),
        XDG_STATE_HOME=str(run_dir / "xdg-state"),
    )

    process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=None)
    raw_lines = [line for line in process.stdout.splitlines() if line.strip()]
    (run_dir / "opencode-events.jsonl").write_text(process.stdout, encoding="utf-8")
    (run_dir / "opencode-stderr.log").write_text(process.stderr, encoding="utf-8")

    events = []
    for line in raw_lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    outcome = parse_events(events, retrieval_tools)
    outcome["returncode"] = process.returncode

    # Translate into the harness-recognized transcript shape.
    tools = sorted({"mcp__retrieval__" + tool for tool in retrieval_tools})
    if retrieval:
        print(json.dumps({"type": "system", "subtype": "init", "tools": tools,
                          "mcp_servers": [{"name": "retrieval", "status": "connected"}]}))
    for name in outcome["unexpected_tools"]:
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "opencode", "name": name, "input": {}}]}}))
    print(json.dumps({
        "type": "result",
        "is_error": outcome["client_error"] is not None or process.returncode != 0,
        "subtype": outcome["client_error"] or ("client_error" if process.returncode != 0 else None),
        "result": outcome["final"],
        "usage": outcome["usage"] or {},
    }))


if __name__ == "__main__":
    main()
