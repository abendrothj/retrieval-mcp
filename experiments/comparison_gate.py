#!/usr/bin/env python3
"""Filter, meter, and record one or more native stdio MCP servers behind a shared budget."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from benchmark import MCP, write_json


def failure(reason):
    return {"isError": True, "content": [{"type": "text", "text": reason}]}


class ComparisonGate:
    """One call/byte budget over every exposed tool, whichever upstream serves it."""

    def __init__(self, routes, max_calls=30, max_bytes=400000):
        if not isinstance(routes, dict) or not routes or not all(
                isinstance(tool, str) and tool and isinstance(upstream, str) and upstream
                for tool, upstream in routes.items()):
            raise ValueError("routes must map nonempty tool names to nonempty upstream ids")
        if type(max_calls) is not int or type(max_bytes) is not int or min(max_calls, max_bytes) < 1:
            raise ValueError("positive integer budgets required")
        self.routes = dict(routes)
        self.visible_tools = set(routes)
        self.max_calls = max_calls
        self.max_bytes = max_bytes
        self.attempts = 0
        self.delivered_bytes = 0
        self.exhausted = False

    def call(self, params, forward):
        started = time.monotonic()
        self.attempts += 1
        name = params.get("name") if isinstance(params, dict) else None
        reason = None
        forwarded = False
        backend_bytes = 0
        if self.exhausted or self.attempts > self.max_calls or self.delivered_bytes >= self.max_bytes:
            reason = "budget_exhausted"
            response = failure(reason)
        elif not isinstance(name, str) or name not in self.visible_tools:
            reason = "unavailable_tool"
            response = failure(reason)
        else:
            forwarded = True
            try:
                response = forward(self.routes[name], params)
            except Exception:
                reason = "backend_error"
                response = failure(reason)
            backend_bytes = len(json.dumps(response, ensure_ascii=False).encode())
            if self.delivered_bytes + backend_bytes > self.max_bytes:
                reason = "response_budget_exhausted"
                response = failure(reason)
                self.exhausted = True
        response_bytes = len(json.dumps(response, ensure_ascii=False).encode())
        self.delivered_bytes += response_bytes
        event = {
            "schema_version": 1,
            "event": "comparison_attempt",
            "timestamp_ms": int(time.time() * 1000),
            "attempt": self.attempts,
            "tool": name,
            "upstream": self.routes.get(name) if isinstance(name, str) else None,
            "arguments": params.get("arguments") if isinstance(params, dict) else None,
            "forwarded": forwarded,
            "reason": reason,
            "tool_error": bool(response.get("isError")),
            "backend_response_bytes": backend_bytes,
            "delivered_bytes": response_bytes,
            "charged_bytes": self.delivered_bytes,
            "latency_ms": (time.monotonic() - started) * 1000,
        }
        return response, event


def serve(config):
    required = {"upstreams", "root", "max_calls", "max_bytes", "timeout", "attempt_log",
                "response_log", "stderr", "tools_path", "server_info_path"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("comparison gate config has unexpected fields")
    upstreams = config["upstreams"]
    fields = {"id", "command", "environment", "visible_tools", "expected_upstream_tools"}
    if not isinstance(upstreams, list) or not upstreams or not all(
            isinstance(entry, dict) and set(entry) == fields for entry in upstreams):
        raise ValueError("every upstream needs id, command, environment, and both tool lists")
    if len({entry["id"] for entry in upstreams}) != len(upstreams):
        raise ValueError("upstream ids must be unique")
    routes = {}
    for entry in upstreams:
        command = entry["command"]
        if not isinstance(command, list) or not command or not all(
                isinstance(part, str) and part for part in command):
            raise ValueError("command must be a nonempty argument array")
        expected = entry["expected_upstream_tools"]
        if (not isinstance(expected, list) or len(expected) != len(set(expected))
                or not all(isinstance(name, str) and name for name in expected)):
            raise ValueError("expected_upstream_tools must contain unique nonempty names")
        if not set(entry["visible_tools"]) <= set(expected):
            raise ValueError("visible tools must be a subset of expected upstream tools")
        for name in entry["visible_tools"]:
            # Two servers answering the same tool name would make routing and attribution guesswork.
            if name in routes:
                raise ValueError(f"tool {name} is exposed by more than one upstream")
            routes[name] = entry["id"]
    gate = ComparisonGate(routes, config["max_calls"], config["max_bytes"])
    with Path(config["attempt_log"]).open("x", encoding="utf-8") as attempts, \
            Path(config["response_log"]).open("x", encoding="utf-8") as responses, \
            Path(config["stderr"]).open("x", encoding="utf-8") as stderr:
        clients = {}
        for entry in upstreams:
            environment = dict(os.environ)
            environment.update(entry["environment"])
            clients[entry["id"]] = MCP(entry["command"], environment, Path(config["root"]),
                                       stderr, config["timeout"])
        try:
            while True:
                line = sys.stdin.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    raise ValueError("MCP request exceeds one MiB")
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise ValueError("invalid JSON-RPC request")
                if "id" not in request:
                    if request.get("method") == "notifications/initialized":
                        for client in clients.values():
                            client.send(request)
                    continue
                try:
                    method = request.get("method")
                    if method == "tools/call":
                        result, event = gate.call(
                            request.get("params", {}),
                            lambda upstream, params: clients[upstream].request("tools/call", params))
                        event["request_id"] = request["id"]
                        attempts.write(json.dumps(event, ensure_ascii=False) + "\n")
                        attempts.flush()
                        responses.write(json.dumps(
                            {"attempt": event["attempt"], "result": result}, ensure_ascii=False) + "\n")
                        responses.flush()
                    elif method == "initialize":
                        results = {entry["id"]: clients[entry["id"]].request(method, request.get("params", {}))
                                   for entry in upstreams}
                        write_json(Path(config["server_info_path"]), results)
                        first = results[upstreams[0]["id"]]
                        # Each upstream states its own retrieval policy; concatenating keeps every
                        # policy the model was actually given, including where they disagree.
                        blocks = [f"[{entry['id']}] {results[entry['id']]['instructions']}"
                                  for entry in upstreams if results[entry["id"]].get("instructions")]
                        result = {**first, "instructions": "\n\n".join(blocks)} if blocks else first
                        capabilities = {"tools": {}}
                        result = {**result, "capabilities": capabilities}
                    elif method == "tools/list":
                        merged = []
                        for entry in upstreams:
                            upstream = clients[entry["id"]].request(method, request.get("params", {}))
                            names = [tool.get("name") for tool in upstream.get("tools", [])]
                            if sorted(names) != sorted(entry["expected_upstream_tools"]):
                                raise ValueError("upstream tool set does not match the pinned system configuration")
                            merged += [tool for tool in upstream["tools"]
                                       if tool["name"] in set(entry["visible_tools"])]
                        result = {"tools": merged}
                        write_json(Path(config["tools_path"]), result)
                    elif method == "ping":
                        for entry in upstreams:
                            result = clients[entry["id"]].request(method, request.get("params", {}))
                    else:
                        raise ValueError("unsupported proxy method")
                    reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                except Exception:
                    reply = {"jsonrpc": "2.0", "id": request["id"],
                             "error": {"code": -32603,
                                       "message": "Comparison transport error; inspect diagnostics"}}
                print(json.dumps(reply, ensure_ascii=False), flush=True)
        finally:
            for client in clients.values():
                client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args()
    serve(json.loads(arguments.config.read_text(encoding="utf-8")))
