#!/usr/bin/env python3
"""Filter, meter, and record one native stdio MCP server for comparisons."""
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
    def __init__(self, visible_tools, max_calls=30, max_bytes=400000):
        if (not isinstance(visible_tools, list) or not visible_tools
                or len(visible_tools) != len(set(visible_tools))
                or not all(isinstance(name, str) and name for name in visible_tools)):
            raise ValueError("visible_tools must contain unique nonempty names")
        if type(max_calls) is not int or type(max_bytes) is not int or min(max_calls, max_bytes) < 1:
            raise ValueError("positive integer budgets required")
        self.visible_tools = set(visible_tools)
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
                response = forward(params)
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
    required = {"command", "root", "environment", "visible_tools", "expected_upstream_tools",
                "max_calls", "max_bytes", "timeout", "attempt_log", "response_log", "stderr",
                "tools_path", "server_info_path"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError("comparison gate config has unexpected fields")
    command = config["command"]
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError("command must be a nonempty argument array")
    expected = config["expected_upstream_tools"]
    if (not isinstance(expected, list) or len(expected) != len(set(expected))
            or not all(isinstance(name, str) and name for name in expected)):
        raise ValueError("expected_upstream_tools must contain unique nonempty names")
    gate = ComparisonGate(config["visible_tools"], config["max_calls"], config["max_bytes"])
    if not gate.visible_tools <= set(expected):
        raise ValueError("visible tools must be a subset of expected upstream tools")
    environment = dict(os.environ)
    environment.update(config["environment"])
    with Path(config["attempt_log"]).open("x", encoding="utf-8") as attempts, \
            Path(config["response_log"]).open("x", encoding="utf-8") as responses, \
            Path(config["stderr"]).open("x", encoding="utf-8") as stderr:
        client = MCP(command, environment, Path(config["root"]), stderr, config["timeout"])
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
                        client.send(request)
                    continue
                try:
                    method = request.get("method")
                    if method == "tools/call":
                        result, event = gate.call(
                            request.get("params", {}), lambda params: client.request("tools/call", params))
                        event["request_id"] = request["id"]
                        attempts.write(json.dumps(event, ensure_ascii=False) + "\n")
                        attempts.flush()
                        responses.write(json.dumps(
                            {"attempt": event["attempt"], "result": result}, ensure_ascii=False) + "\n")
                        responses.flush()
                    elif method == "initialize":
                        result = client.request(method, request.get("params", {}))
                        write_json(Path(config["server_info_path"]), result)
                    elif method == "tools/list":
                        upstream = client.request(method, request.get("params", {}))
                        names = [tool.get("name") for tool in upstream.get("tools", [])]
                        if sorted(names) != sorted(expected):
                            raise ValueError("upstream tool set does not match the pinned system configuration")
                        result = {**upstream, "tools": [
                            tool for tool in upstream["tools"] if tool["name"] in gate.visible_tools
                        ]}
                        write_json(Path(config["tools_path"]), result)
                    elif method == "ping":
                        result = client.request(method, request.get("params", {}))
                    else:
                        raise ValueError("unsupported proxy method")
                    reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                except Exception:
                    reply = {"jsonrpc": "2.0", "id": request["id"],
                             "error": {"code": -32603,
                                       "message": "Comparison transport error; inspect diagnostics"}}
                print(json.dumps(reply, ensure_ascii=False), flush=True)
        finally:
            client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args()
    serve(json.loads(arguments.config.read_text(encoding="utf-8")))
