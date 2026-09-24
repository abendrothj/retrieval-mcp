#!/usr/bin/env python3
"""One tool that takes a plan, so a dependent chain costs one round trip instead of three.

The whole measured gap against a shell agent is composition. A dependent hop is ~15,000 input
tokens and a 20 KB payload is ~2,400 (`runs/token-metric-20260921`), and across the archive a
shell agent chains **2.03** operations per repository call while this server's four-tool surface
gets **1.02**: it thinks by running another command, and an MCP call answers one question per
round trip by construction. Putting the same four operations in a shell as subcommands did not fix
it - `runs/cli-transport-20260924` measured 1.16 operations per CLI-bearing call, the model almost
always running one `retrieval` per call. So the operations being *available* in a shell is not the
same as the interface *being* composable.

This proxy makes composition the unit. It exposes exactly one tool, `retrieve`, whose argument is
a list of steps; it runs them against the real server in one client round trip and returns the
concatenated result. A one-step plan is exactly today's behaviour, so nothing is taken away.

    {"steps": [
      {"tool": "search_concept", "arguments": {"query": "lease renewal expiry"}},
      {"tool": "find_callers",   "arguments": {"name": "$1.symbol_name"}},
      {"tool": "read_source",    "arguments": {"path": "$2.path", "start_line": "$2.start_line",
                                               "end_line": "$2.end_line"}}
    ]}

`$N.field` is that field of the first row of step N's results, which is what a shell pipeline does
with `| head -1 | cut -f`, and `$N.symbol_name` is the bare name inside a `path::Name` identity,
which is the form `find_callers` accepts. References are resolved by this proxy from rows the real server
returned, so no step is ever guessed.

**Why this is not the composition that already failed.** `runs/composed-callers-20260922` gave the
server a `find_callers` that resolved a description itself and it scored 3 of 8 against an agent
two-hop that is near ceiling; the recorded lesson is that the model's two hops are error-correcting
and removing the check removes the correction. Here the *model* still writes every step and every
reference - it decides the chain, reads the whole chain's output, and can plan again. What is
removed is the round trip between steps, not the model's judgement. That is the one-variable
difference and it is why this is worth a run.

The registered mechanism bar is operations per call above 2.03, the shell agent's own chaining. If
the model writes one-step plans anyway, the hop term is its reasoning rather than the channel and
there is nothing here to win.

No model, no network. The real server is unmodified underneath.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

TOOL = "retrieve"
REFERENCE = re.compile(r"^\$(\d+)\.([A-Za-z_][A-Za-z0-9_.]*)$")
SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string",
                             "enum": ["search_exact", "search_concept", "find_callers",
                                      "read_source"]},
                    "arguments": {"type": "object"},
                },
                "required": ["tool", "arguments"],
            },
        }
    },
    "required": ["steps"],
}


def annotations(tools):
    """The annotations a plan of these steps deserves, derived from the steps' own.

    Codex auto-approves an MCP tool that declares `readOnlyHint` and refuses one that declares
    nothing: under `--sandbox read-only` with the approval policy at `never`, an unannotated tool
    answers every call with "MCP tool call requires approval, but approval policy is never". That
    is not a behavioural result about composition - it is an arm that cannot run - and it cost two
    attempts of this study before the rollouts were read (the model wrote well-formed plans on its
    first action and was blocked five times).

    A plan is read-only only if every step it can hold is, so this takes the conjunction rather
    than asserting the flag.
    """
    present = [tool.get("annotations") or {} for tool in tools]
    if not present:
        return {}
    return {
        "readOnlyHint": all(a.get("readOnlyHint") is True for a in present),
        "destructiveHint": any(a.get("destructiveHint") is True for a in present),
        "openWorldHint": any(a.get("openWorldHint") is True for a in present),
    }


def description(tools):
    """The one tool's description, built from the real tools' own descriptions.

    Authoring prose that tells an arm its tools are good is what contaminated 2,109 archived
    trials, so this is generated rather than written: the routing sentences are the server's own.
    """
    lines = [
        # Usage documentation, which is what a shipped tool description is, and deliberately not
        # a claim about outcomes. "Put dependent steps in one call" tells the model how the
        # interface works; "these tools are cheaper and more precise than reading files at random"
        # is a claim about the thing under measurement, and that sentence - shipped in
        # skills/retrieval-mcp/SKILL.md - is what contaminated 2,109 archived trials. The first is
        # legitimate and this arm carries it; the second is not and no arm carries it.
        #
        # This makes the arm the UPPER BOUND on the interface: an undocumented variant can only do
        # worse, so if this loses to a shell agent the interface is finished either way. Splitting
        # the win between the prose and the interface is a later arm, not this one.
        "Run one or more retrieval steps in a single call. Steps execute in order and a later "
        "step may reference an earlier one's first row as $N.field ($1.symbol is that row's "
        "identity, $1.symbol_name the bare name a find_callers step takes, $2.path and $2.line a "
        "call site), resolved here from the rows the index returned. A plan may hold one step or "
        "several: concept lookup, then callers of the symbol it named, then the source at the "
        "call site, is one call. When the steps you need depend on each other, put them in one "
        "plan rather than issuing them one at a time.",
        "",
        "Steps available:",
    ]
    for tool in tools:
        text = (tool.get("description") or "").strip().split("\n")[0]
        lines.append(f"- {tool['name']}: {text}")
    return "\n".join(lines)


STEP_TOOLS = ("search_exact", "search_concept", "find_callers", "read_source")


def rewrite_instructions(text):
    """Say the server's own routing in terms of plan steps, because the tools are not callable.

    Codex prefixes a server's `initialize` instructions to every tool description, and this
    server's instructions route the model to `search_exact`, `find_callers`, `search_concept` and
    `read_source` as tools. In this arm they are not tools - they are step kinds inside one
    `retrieve` call - so an unrewritten arm tells the model to call four things it cannot see. It
    then goes to the shell instead: measured, 0 MCP calls across 7 trials against the four-tool
    arm's 7.67, with the tool found in the rollout and never used.

    The routing advice itself is not rewritten, only the grammar of how a tool is reached. Nothing
    here claims the interface is good; the one authored sentence states the call shape and the
    reference syntax, which is the interface's contract and not persuasion about it.
    """
    for name in STEP_TOOLS:
        text = text.replace(name, f"a `{name}` step")
    preamble = (
        "This server exposes one tool, `retrieve`. It takes `steps`: a list of "
        "{tool, arguments} objects, run in order within a single call, where `tool` is one of "
        + ", ".join(STEP_TOOLS) + ". A later step may reference an earlier one's first row as "
        "$N.field - $1.symbol is that row's identity, $1.symbol_name the bare name a "
        "`find_callers` step takes, $2.path and $2.line a call site - and references are resolved "
        "here from the rows the index returned. A plan may hold one step or several.\n"
        "The routing below is this server's own, with each tool named as the step that runs it.\n\n")
    return preamble + text


def resolve(value, results):
    """Substitute `$N.field` from the first row of step N, recursively through the arguments.

    A dotted path walks nested objects, and a single name unwraps one level when the object it
    lands on repeats that name. Both exist because of how the server renders a row: the `symbol`
    cell is an object carrying `kind`, the callee counts and a `symbol` string, so the reference an
    agent naturally writes - `$1.symbol` - has to mean the identity rather than the object around
    it. Without the unwrap the next step is called with a map where it wants a string, and the
    server answers `invalid type: map, expected a string` - which is how this was found.
    """
    if isinstance(value, str):
        match = REFERENCE.match(value)
        if not match:
            return value
        index, path = int(match.group(1)), match.group(2).split(".")
        if not 1 <= index <= len(results):
            raise ValueError(f"step reference {value} names step {index}, and {len(results)} "
                             f"step(s) have run")
        rows = results[index - 1]
        if not rows:
            raise ValueError(f"step reference {value} names step {index}, which returned no rows")
        current = rows[0]
        # `$N.symbol_name` is the identity's last component, because `find_callers` takes a bare
        # name and a `path::Name` identity is `unknown_symbol` to it - measured, not assumed. This
        # is the `cut` a shell pipeline would write, and it is the only derived field.
        derive_name = len(path) == 1 and path[0].endswith("_name")
        if derive_name:
            path = [path[0][: -len("_name")]]
        for position, field in enumerate(path):
            if not isinstance(current, dict):
                raise ValueError(f"step reference {value} walks into {type(current).__name__} at "
                                 f"{'.'.join(path[:position])!r}")
            if field not in current:
                raise ValueError(f"step reference {value} names field {field!r}, and step "
                                 f"{index}'s first row has {sorted(current)} there")
            current = current[field]
        # One unwrap, and only when the object repeats the name asked for.
        if isinstance(current, dict) and path[-1] in current:
            current = current[path[-1]]
        if derive_name:
            if not isinstance(current, str):
                raise ValueError(f"step reference {value} wants the name of a "
                                 f"{type(current).__name__}")
            current = current.rsplit("::", 1)[-1]
        return current
    if isinstance(value, list):
        return [resolve(item, results) for item in value]
    if isinstance(value, dict):
        return {key: resolve(item, results) for key, item in value.items()}
    return value


def rows_of(payload):
    """The structured rows a tool result carries, or [] - never a guess from rendered text."""
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return []
    rows = structured.get("results")
    return rows if isinstance(rows, list) else []


def text_of(payload):
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    for block in result.get("content") or []:
        if block.get("type") == "text":
            return block.get("text", "")
    return ""


class Child:
    """The real server, spoken to synchronously. Anything it says unprompted is forwarded."""

    def __init__(self, command, out):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.out = out
        self.next_id = 100000

    def send(self, payload):
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def call(self, name, arguments):
        self.next_id += 1
        identifier = self.next_id
        self.send({"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}})
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise ValueError(f"the server closed while answering {name}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == identifier:
                return payload
            # A notification or an unrelated answer: it belongs to the client, not to this plan.
            self.out.write(line)
            self.out.flush()

    def relay(self, line):
        """Pass a client message through and return the server's matching answer, if any."""
        self.send(json.loads(line))
        payload = json.loads(line)
        if payload.get("id") is None:
            return None
        while True:
            answer = self.process.stdout.readline()
            if not answer:
                return None
            try:
                parsed = json.loads(answer)
            except json.JSONDecodeError:
                continue
            if parsed.get("id") == payload.get("id"):
                return parsed
            self.out.write(answer)
            self.out.flush()


def run_plan(child, steps, log):
    """Execute the plan, returning one combined result payload."""
    rendered, rows_per_step, operations = [], [], 0
    stopped = None
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or "tool" not in step or "arguments" not in step:
            raise ValueError(f"step {position} is not a {{tool, arguments}} object")
        # A step whose reference cannot be resolved, or that the server refuses, ends the plan -
        # but the steps that did run are returned. Throwing the whole call away would hand back a
        # round trip's worth of evidence for nothing, which is the cost this interface exists to
        # avoid; the model sees how far the chain got and re-plans from real rows.
        try:
            arguments = resolve(step["arguments"], rows_per_step)
        except ValueError as failure:
            stopped = f"step {position} ({step['tool']}) did not run: {failure}"
            break
        answer = child.call(step["tool"], arguments)
        operations += 1
        error = answer.get("error")
        rows_per_step.append(rows_of(answer))
        rendered.append(f"step {position}: {step['tool']} "
                        f"{json.dumps(arguments, sort_keys=True)}\n{text_of(answer)}")
        if error:
            stopped = f"step {position} ({step['tool']}) failed: {error}"
            break
    if stopped:
        rendered.append(f"plan stopped: {stopped}")
    print(f"compose_server: plan of {operations} operation(s)"
          f"{', stopped' if stopped else ''}", file=log, flush=True)
    return operations, {
        "content": [{"type": "text", "text": "\n\n".join(rendered)}],
        "structuredContent": {"steps": [{"results": rows} for rows in rows_per_step],
                              "operations": operations,
                              **({"stopped": stopped} if stopped else {})},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", type=Path, required=True, help="the real server binary")
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="-- followed by the real server's own arguments")
    args = parser.parse_args()
    server_args = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    child = Child([str(args.server), *server_args], sys.stdout)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                child.process.stdin.write(line)
                child.process.stdin.flush()
                continue
            method, params = payload.get("method"), payload.get("params") or {}
            if method == "tools/call" and params.get("name") == TOOL:
                identifier = payload.get("id")
                try:
                    steps = (params.get("arguments") or {}).get("steps")
                    if not isinstance(steps, list) or not steps:
                        raise ValueError("retrieve takes a non-empty `steps` array")
                    _, result = run_plan(child, steps, sys.stderr)
                    answer = {"jsonrpc": "2.0", "id": identifier, "result": result}
                except ValueError as failure:
                    answer = {"jsonrpc": "2.0", "id": identifier,
                              "result": {"isError": True,
                                         "content": [{"type": "text",
                                                      "text": f"error: {failure}"}]}}
                sys.stdout.write(json.dumps(answer) + "\n")
                sys.stdout.flush()
                continue
            answer = child.relay(line)
            if answer is None:
                continue
            if method == "initialize":
                result = answer.get("result")
                if isinstance(result, dict) and isinstance(result.get("instructions"), str):
                    result["instructions"] = rewrite_instructions(result["instructions"])
            if method == "tools/list":
                tools = ((answer.get("result") or {}).get("tools")) or []
                answer["result"]["tools"] = [{"name": TOOL, "description": description(tools),
                                              "inputSchema": SCHEMA,
                                              "annotations": annotations(tools)}]
            sys.stdout.write(json.dumps(answer) + "\n")
            sys.stdout.flush()
    except BrokenPipeError:
        pass
    child.process.stdin.close()
    return child.process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
