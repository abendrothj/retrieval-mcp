#!/usr/bin/env python3
"""Serial stdio policy boundary for the existing Rust MCP server (no model client)."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from benchmark import MCP, TOOLS


def failure(message):
    return {"isError":True, "content":[{"type":"text", "text":message}]}


class PolicyGate:
    def __init__(self, profile, policy, max_calls=20, max_bytes=200000):
        if profile not in TOOLS or policy not in ("free", "lexical_first", "concept_first"):
            raise ValueError("invalid profile or policy")
        self.required = {"free":None, "lexical_first":"search_exact", "concept_first":"search_concept"}[policy]
        if self.required and self.required not in TOOLS[profile]:
            raise ValueError("required first tool is unavailable")
        if type(max_calls) is not int or type(max_bytes) is not int or min(max_calls, max_bytes) < 1:
            raise ValueError("positive integer budgets required")
        self.allowed, self.policy = TOOLS[profile], policy
        self.max_calls, self.max_bytes = max_calls, max_bytes
        self.attempts, self.bytes = 0, 0
        self.first_attempt = None
        self.first_forwarded = None
        self.violations = 0

    def call(self, params, forward):
        started = time.monotonic()
        self.attempts += 1
        name = params.get("name") if isinstance(params, dict) else None
        if self.attempts == 1:
            self.first_attempt = name
        reason = None
        forwarded = False
        backend_bytes = 0
        if self.attempts > self.max_calls or self.bytes >= self.max_bytes:
            reason = "budget_exhausted"
        elif not isinstance(name, str) or name not in self.allowed:
            reason = "unavailable_tool"
        elif self.first_forwarded is None and self.required and name != self.required:
            self.violations += 1
            reason = "first_tool_policy_violation"
        if reason:
            response = failure(f"{reason}; required first tool: {self.required or 'any available tool'}. Attempts count against budget.")
        else:
            # A required-tool attempt satisfies ordering even when its arguments fail.
            # Syntax success is measured separately. Calls are serialized until response.
            self.first_forwarded = self.first_forwarded or name
            forwarded = True
            try:
                response = forward(params)
            except Exception:
                reason = "backend_error"
                response = failure("backend_error; inspect retained server diagnostics")
            backend_bytes = len(json.dumps(response).encode())
            if self.bytes + backend_bytes > self.max_bytes:
                reason = "response_budget_exhausted"
                response = failure("response_budget_exhausted; no further retrieval is permitted")
                self.bytes = self.max_bytes
        delivered_bytes = len(json.dumps(response).encode())
        self.bytes += delivered_bytes
        event = {"schema_version":1, "event":"policy_attempt", "timestamp_ms":int(time.time()*1000),
                 "attempt":self.attempts, "tool":name, "arguments":params.get("arguments") if isinstance(params, dict) else None,
                 "policy":self.policy, "first_attempt":self.first_attempt, "first_forwarded":self.first_forwarded,
                 "policy_violations":self.violations, "forwarded":forwarded, "reason":reason,
                 "tool_error":bool(response.get("isError")), "backend_response_bytes":backend_bytes,
                 "delivered_bytes":delivered_bytes, "charged_bytes":self.bytes,
                 "latency_ms":(time.monotonic()-started)*1000}
        return response, event


def serve(config):
    gate = PolicyGate(config["profile"], config["policy"], config["max_calls"], config["max_bytes"])
    # Serial forwarding is intentional: no parallel first calls can bypass the gate.
    # This adapter supports the server's request/response tools, not bidirectional sampling.
    with Path(config["gate_log"]).open("x") as log, Path(config["stderr"]).open("x") as stderr:
        # Optional full-response capture for probe scoring; off by default because payloads are large.
        responses = Path(config["response_log"]).open("x") if config.get("response_log") else None
        client = MCP(config["command"], os.environ.copy(), Path(config["root"]), stderr, config["timeout"])
        try:
            while True:
                line = sys.stdin.readline(1024*1024 + 1)
                if not line:
                    break
                if len(line) > 1024*1024:
                    raise ValueError("MCP request exceeds one MiB")
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise ValueError("invalid JSON-RPC request")
                if "id" not in request:
                    if request.get("method") == "notifications/initialized":
                        client.send(request)
                    continue
                try:
                    if request["method"] == "tools/call":
                        result, event = gate.call(request.get("params", {}), lambda p:client.request("tools/call", p))
                        event["request_id"] = request["id"]
                        log.write(json.dumps(event) + "\n")
                        log.flush()
                        if responses:
                            responses.write(json.dumps({"attempt":event["attempt"], "result":result}) + "\n")
                            responses.flush()
                    elif request["method"] in ("initialize", "tools/list", "ping"):
                        result = client.request(request["method"], request.get("params", {}))
                        if request["method"] == "tools/list" and sorted(t["name"] for t in result["tools"]) != sorted(gate.allowed):
                            raise ValueError("tool profile mismatch")
                    else:
                        raise ValueError("unsupported proxy method")
                    reply = {"jsonrpc":"2.0", "id":request["id"], "result":result}
                except Exception:
                    reply = {"jsonrpc":"2.0", "id":request["id"], "error":{"code":-32603, "message":"Policy transport error; inspect diagnostics"}}
                print(json.dumps(reply), flush=True)
        finally:
            client.close()
            if responses:
                responses.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    serve(json.loads(args.config.read_text()))
