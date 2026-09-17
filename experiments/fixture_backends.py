"""The two fixture processes the harness tests drive: a scripted agent and a scripted
semantic backend. Neither calls a model. They live beside the instruments rather than inside a
test module because `factor_runner.py` and `competency_runner.py` launch the semantic one as a
subprocess, and an instrument must not depend on the test package to run.

    python3 experiments/fixture_backends.py --fake-agent <mcp-config.json>
    python3 experiments/fixture_backends.py --fake-semantic   # request on stdin, reply on stdout
"""
import json
import os
from pathlib import Path
import sys

import benchmark


def fake_agent(config):
    """Only for transport tests; never described as a model benchmark."""
    settings = json.loads(Path(config).read_text())["mcpServers"]["retrieval"]
    env = dict(os.environ, **settings.get("env", {}))
    command = [settings["command"], *settings["args"]]
    with open("fake-server-stderr.log", "w") as stderr:
        client = benchmark.MCP(command, env, Path.cwd(), stderr, 10)
        try:
            client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "fake-agent", "version": "1"}})
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = [tool["name"] for tool in client.request("tools/list", {})["tools"]]
            if "find_callers" in tools:
                name, arguments = "find_symbol", {"name": "delay"}
            elif "search_concept" in tools:
                name, arguments = "search_concept", {"query": "waiting longer after failures"}
            else:
                name, arguments = "search_exact", {"query": "delay"}
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": f"mcp__retrieval__{name}", "input": arguments}]}}), flush=True)
            result = client.request("tools/call", {"name": name, "arguments": arguments})
            print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": result}]}}), flush=True)
            if result.get("isError"):
                raise RuntimeError(result)
            result = client.request("tools/call", {"name": "read_source", "arguments": {"path": "policy.py"}})
            if result.get("isError"):
                raise RuntimeError(result)
            print(json.dumps({"type": "result", "result": "32", "usage": {"input_tokens": 42},
                              "is_error": False, "test_only": True}), flush=True)
        finally:
            client.close()


def comparison_server(name="native_search"):
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "comparison-fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "description": "Return fixture evidence.",
                 "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}},
                {"name": f"{name}_admin", "description": "Must be filtered.",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}
        elif method == "tools/call":
            result = {"isError": False, "content": [{"type": "text", "text": f"fixture evidence from {name}"}],
                      "structuredContent": {"answer": "ok", "served_by": name}}
        elif method == "ping":
            result = {}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "unsupported"}]}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def comparison_agent(config_path, answer):
    servers = json.loads(Path(config_path).read_text())["mcpServers"]
    if "retrieval" not in servers:
        print(json.dumps({"type": "result", "is_error": False, "result": answer,
                          "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
        return
    server = servers["retrieval"]
    with open(os.devnull, "w") as stderr:
        client = benchmark.MCP([server["command"], *server["args"]], os.environ.copy(), Path.cwd(), stderr, 20)
        try:
            client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                          "clientInfo": {"name": "comparison-agent-fixture", "version": "1"}})
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            names = [tool["name"] for tool in client.request("tools/list", {})["tools"]]
            if any(name.endswith("_admin") for name in names) or not names:
                raise ValueError("gate did not filter administrative tools")
            print(json.dumps({"type": "system", "subtype": "init",
                              "tools": [f"mcp__retrieval__{name}" for name in names],
                              "mcp_servers": [{"name": "retrieval", "status": "connected"}]}), flush=True)
            # Call every exposed tool so a bundled arm exercises both upstreams on one budget.
            for index, name in enumerate(names, 1):
                print(json.dumps({"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": str(index), "name": f"mcp__retrieval__{name}",
                    "input": {"query": "fixture"}}]}}), flush=True)
                result = client.request("tools/call", {"name": name, "arguments": {"query": "fixture"}})
                print(json.dumps({"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": str(index), "content": result}]}}), flush=True)
            print(json.dumps({"type": "result", "is_error": False, "result": answer,
                              "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
        finally:
            client.close()


def refusing_agent():
    """An agent whose provider refuses to serve: the shape opencode_wrapper emits for a 402."""
    print(json.dumps({"type": "result", "is_error": True, "subtype": "provider_error:402",
                      "provider_detail": "Insufficient Balance", "result": None,
                      "usage": {}}), flush=True)


SEMANTIC_REPLY = {"protocol_version": 1, "backend": "test-only", "index_note": "fixture, no model",
                  "has_more": False,
                  "results": [{"path": "policy.py", "start_line": 1, "end_line": 2, "score": 0.5}]}


def main(argv):
    if argv[1:2] == ["--fake-agent"]:
        sys.stdin.read()
        fake_agent(argv[2])
    elif argv[1:2] == ["--fake-semantic"]:
        json.load(sys.stdin)
        print(json.dumps(SEMANTIC_REPLY))
    elif argv[1:2] == ["--fake-server"]:
        comparison_server(argv[2])
    elif argv[1:2] == ["--comparison-agent"]:
        comparison_agent(argv[2], argv[3])
    elif argv[1:2] == ["--refusing-agent"]:
        refusing_agent()
    else:
        raise SystemExit("usage: fixture_backends.py --fake-agent <config> | --fake-semantic | "
                         "--fake-server <tool> | --comparison-agent <config> <answer> | "
                         "--refusing-agent")


if __name__ == "__main__":
    main(sys.argv)
