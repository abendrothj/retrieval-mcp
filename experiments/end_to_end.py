#!/usr/bin/env python3
"""Score a multi-step comparison run on quality-adjusted context consumption.

Every number here is recomputed offline from each trial's raw OpenCode event stream, so the
accounting is identical across arms: an MCP tool result and a native `read` result are both
just bytes a tool put into the model's context. No model is called.

The metric the project actually cares about is not total tokens but tokens spent after the
evidence was already on screen: `tokens_after_first_hit`. A system that surfaces the gold file
in its first call and then needs six more reads to commit is not a good retrieval layer.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics

import quality_pass

# Tools that put repository content into the context window, whichever arm serves them.
RETRIEVAL_TOOLS = {"read", "grep", "glob", "list", "bash", "lsp"}
READ_TOOLS = {"read", "read_source", "get_code_snippet", "zvec_grep_rg"}


def events(trial):
    """(stream, client) for whichever agent client produced this trial.

    The Claude client has no side-channel event file: its stream-json output *is* the transcript,
    which every client writes. It is therefore tried last, and only when a stream-json `system`
    init or `result` envelope identifies it, so a Codex transcript is never misread as Claude.
    """
    for name, client in (("opencode-events.jsonl", "opencode"), ("codex-events.jsonl", "codex"),
                         ("transcript.jsonl", "claude")):
        path = trial / name
        if not path.is_file():
            continue
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        if client == "claude" and not any(
                event.get("type") == "assistant" and isinstance(event.get("message"), dict)
                for event in out):
            return [], None
        return out, client
    return [], None


def tool_name(part):
    name = part.get("tool") or ""
    return name[len("retrieval_"):] if name.startswith("retrieval_") else name


def gold_markers(task):
    """The file paths and symbol names that prove the answer was on screen."""
    paths, symbols = set(), set()

    def walk(value):
        if isinstance(value, str):
            if "::" in value:
                head, _, tail = value.partition("::")
                paths.add(head)
                symbols.add(tail.split("::")[-1])
            elif "/" in value:
                paths.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(task["expected_json"]["answer"])
    walk(task.get("acceptable_symbols") or [])
    return paths, symbols


def calls_from(stream, client):
    """One uniform record per tool call: name, bytes it put in context, latency when known."""
    records = []
    if client == "opencode":
        for event in stream:
            if event.get("type") != "tool_use":
                continue
            part = event.get("part") or {}
            state = part.get("state") or {}
            output = state.get("output")
            body = output if isinstance(output, str) else json.dumps(output or "")
            times = state.get("time") or {}
            name = tool_name(part)
            records.append({
                "name": name, "body": body,
                "read": name in READ_TOOLS,
                "latency_ms": (times["end"] - times["start"])
                if times.get("start") and times.get("end") else None,
            })
    elif client == "codex":
        for event in stream:
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if item.get("type") == "command_execution":
                command = item.get("command") or ""
                records.append({
                    "name": "shell", "body": item.get("aggregated_output") or "",
                    # Codex runs one shell; a command that pages a file is a read whatever else
                    # it does, and the same command may also search.
                    "read": any(token in command for token in ("cat ", "sed -n", "head ", "tail ")),
                    "latency_ms": None,
                })
            elif item.get("type") == "mcp_tool_call":
                body = item.get("result") or item.get("output") or ""
                name = item.get("tool") or item.get("name") or "mcp"
                records.append({
                    "name": name,
                    "body": body if isinstance(body, str) else json.dumps(body),
                    "read": name in READ_TOOLS, "latency_ms": None,
                })
    elif client == "claude":
        # Claude names a tool only on the `tool_use` block; the payload arrives later as a
        # `tool_result` on a user message, matched back by `tool_use_id`.
        pending = {}
        for event in stream:
            message = event.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    name = block.get("name") or ""
                    pending[block.get("id")] = name.rsplit("__", 1)[-1] if "__" in name else name
                elif block.get("type") == "tool_result":
                    name = pending.pop(block.get("tool_use_id"), "mcp")
                    body = block.get("content")
                    records.append({
                        "name": name,
                        "body": body if isinstance(body, str) else json.dumps(body or ""),
                        "read": name in READ_TOOLS, "latency_ms": None,
                    })
    return records


def steps_from(stream, client):
    """Per-step token accounting where the client reports it; one cumulative step where it does not."""
    steps = []
    if client == "opencode":
        for event in stream:
            if event.get("type") != "step_finish":
                continue
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            steps.append({"input": int(tokens.get("input") or 0),
                          "output": int(tokens.get("output") or 0),
                          "reasoning": int(tokens.get("reasoning") or 0),
                          "cache_read": int(cache.get("read") or 0),
                          "cost": float(part.get("cost") or 0.0)})
    elif client == "codex":
        # Codex reports usage once per turn, so the profile is a total, never a per-step curve.
        for event in stream:
            if event.get("type") != "turn.completed":
                continue
            counts = event.get("usage") or {}
            steps.append({"input": int(counts.get("input_tokens") or 0),
                          "output": int(counts.get("output_tokens") or 0),
                          "reasoning": int(counts.get("reasoning_output_tokens") or 0),
                          "cache_read": int(counts.get("cached_input_tokens") or 0),
                          "cost": 0.0})
    elif client == "claude":
        # One usage object is repeated across every block of the same API response, so steps are
        # deduplicated by message id. Deduplicated per-message usage sums to the final result
        # usage, which is why the total is taken from the steps rather than the result envelope.
        seen = set()
        for event in stream:
            if event.get("type") != "assistant":
                continue
            message = event.get("message") or {}
            identity = message.get("id")
            if identity in seen:
                continue
            seen.add(identity)
            counts = message.get("usage") or {}
            cache_read = int(counts.get("cache_read_input_tokens") or 0)
            steps.append({
                # Context actually carried this turn: fresh prompt, newly cached prefix, and the
                # prefix replayed from cache. Claude bills these separately; the study counts what
                # the model had to be sent.
                "input": int(counts.get("input_tokens") or 0)
                + int(counts.get("cache_creation_input_tokens") or 0) + cache_read,
                "output": int(counts.get("output_tokens") or 0),
                "reasoning": int((counts.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
                "cache_read": cache_read,
                "cost": 0.0,
            })
        for event in stream:
            if event.get("type") == "result" and steps:
                steps[-1]["cost"] = float(event.get("total_cost_usd") or 0.0)
                thinking = int((
                    (event.get("usage") or {}).get("output_tokens_details") or {}
                ).get("thinking_tokens") or 0)
                if thinking and not any(step["reasoning"] for step in steps):
                    steps[-1]["reasoning"] = thinking
    return steps


def context_token_turns(records):
    """Tokens a tool payload occupies, multiplied by the model turns that must carry it.

    A transcript agent re-sends every earlier result with every later turn, so a result is not
    paid for once. A payload returned early and followed by four turns costs roughly five times
    what the same payload costs when the model answers immediately. Bytes are converted at four
    per token, which is approximate: this ranks payloads against each other, it is not billing.
    """
    total = len(records)
    return sum(len(record["body"].encode()) / 4 * (total - position)
               for position, record in enumerate(records))


def gold_identities(task):
    """Every (path, leaf) the gold asserts, so evidence can be judged per required claim."""
    found = []
    for value in quality_pass.flatten(task["expected_json"]["answer"]):
        path, _, rest = value.partition("::")
        if rest:
            found.append((path, rest.split("::")[-1]))
    return found


def unretrieved(text, pairs):
    """Gold identities that never appeared in any tool result: what the model could not have seen.

    This is the safety half of a closure metric. Saving turns by answering sooner is only a win if
    the evidence was already present; an answer produced while a required identity was never on
    screen is a premature stop, and it costs correctness rather than tokens. Visibility is textual
    and therefore generous - it can only understate this count, never invent one.
    """
    return [pair for pair in pairs if pair[0] not in text or pair[1] not in text]


def trial_metrics(trial, task):
    """Replay one trial's event stream into a context-consumption profile."""
    paths, symbols = gold_markers(task)
    stream, client = events(trial)
    records = calls_from(stream, client)
    steps = steps_from(stream, client)
    hit = None
    for position, record in enumerate(records):
        body = record["body"]
        if any(path in body for path in paths) or any(symbol in body for symbol in symbols):
            hit = position
            break
    pairs = gold_identities(task)
    missing = unretrieved("\n".join(record["body"] for record in records), pairs)
    latencies = [record["latency_ms"] for record in records if record["latency_ms"] is not None]
    total = {key: sum(step[key] for step in steps)
             for key in ("input", "output", "reasoning", "cache_read", "cost")}
    # Per-step tokens exist only where the client reports them; with one cumulative step the
    # question "how much did it spend after the evidence arrived?" is answered in calls and bytes.
    per_step = len(steps) > 1
    after = ({key: sum(step[key] for step in steps[hit:]) for key in
              ("input", "output", "reasoning", "cost")}
             if hit is not None and per_step else
             {key: None for key in ("input", "output", "reasoning", "cost")})
    return {
        "client": client,
        "steps": len(steps),
        "calls": len(records),
        "reads": sum(1 for record in records if record["read"]),
        "tools": dict(Counter(record["name"] for record in records)),
        "retrieval_bytes": sum(len(record["body"].encode()) for record in records),
        "context_token_turns": round(context_token_turns(records)),
        "retrieval_latency_ms": sum(latencies) if latencies else None,
        "tokens": total,
        "first_hit_call": hit,
        "calls_to_first_hit": None if hit is None else hit + 1,
        "calls_after_first_hit": None if hit is None else len(records) - hit - 1,
        "bytes_after_first_hit": None if hit is None else
        sum(len(record["body"].encode()) for record in records[hit + 1:]),
        "tokens_after_first_hit": after,
        "gold_identities": len(pairs),
        "unretrieved_identities": ["::".join(pair) for pair in missing],
        "answered_without_evidence": bool(pairs) and bool(missing),
    }


def summarize(rows):
    def stat(key, getter=None):
        values = [getter(row) if getter else row[key] for row in rows]
        values = [value for value in values if value is not None]
        return {"median": round(statistics.median(values), 1) if values else None,
                "mean": round(statistics.fmean(values), 1) if values else None,
                "n": len(values)}

    resolved = [row for row in rows if row["resolved_correct"]]
    return {
        "trials": len(rows),
        "completed": sum(1 for row in rows if row["status"] == "completed"),
        "correct": sum(1 for row in rows if row["correct"]),
        "resolved_correct": len(resolved),
        "resolved_credit_mean": round(statistics.fmean(
            [row["resolved_credit"] for row in rows]), 3) if rows else None,
        "budget_exhausted": sum(1 for row in rows if row.get("budget_exhausted")),
        "input_tokens": stat("input", lambda r: r["tokens"]["input"]),
        "output_tokens": stat("output", lambda r: r["tokens"]["output"]),
        "reasoning_tokens": stat("reasoning", lambda r: r["tokens"]["reasoning"]),
        "retrieval_bytes": stat("retrieval_bytes"),
        "context_token_turns": stat("context_token_turns"),
        "retrieval_calls": stat("calls"),
        "source_reads": stat("reads"),
        "retrieval_latency_ms": stat("retrieval_latency_ms"),
        "wall_time_ms": stat("wall_time_ms"),
        "cost_usd": round(sum(row["tokens"]["cost"] for row in rows), 4),
        "calls_to_first_hit": stat("calls_to_first_hit"),
        "calls_after_first_hit": stat("calls_after_first_hit"),
        "bytes_after_first_hit": stat("bytes_after_first_hit"),
        "input_tokens_after_first_hit": stat(
            "after_input", lambda r: r["tokens_after_first_hit"]["input"]),
        "trials_with_hit": sum(1 for row in rows if row["first_hit_call"] is not None),
        # Permanent safety line. An efficiency change that saves tokens while raising this has
        # bought context by answering without the evidence, which is a regression whatever the
        # token columns say. `wrong_without_evidence` is the subset that was also graded wrong.
        "answered_without_evidence": sum(1 for row in rows if row.get("answered_without_evidence")),
        "wrong_without_evidence": sum(1 for row in rows if row.get("answered_without_evidence")
                                      and not row["resolved_correct"]),
        # The comparison that matters: cost of the evidence, and cost of everything after it.
        "resolved_input_tokens_median": round(statistics.median(
            [row["tokens"]["input"] for row in resolved]), 1) if resolved else None,
        "resolved_retrieval_bytes_median": round(statistics.median(
            [row["retrieval_bytes"] for row in resolved]), 1) if resolved else None,
    }


def report(args):
    run = args.run.resolve(strict=True)
    tasks = {task["id"]: task for task in json.loads(args.questions.read_text(encoding="utf-8"))}
    rows = []
    for trial in sorted(run.glob("trial-*")):
        state_path = trial / "run.json"
        if not state_path.is_file():
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["status"] == "provider_error":
            # The provider failed, not the arm; such a trial is evidence about nothing.
            continue
        task = tasks.get(state["task_id"])
        if task is None:
            raise KeyError(f"{state['task_id']} is not in the question set")
        row = {
            "trial": trial.name,
            "system": state["system"],
            "task_id": state["task_id"],
            "repetition": state["repetition"],
            "status": state["status"],
            "correct": bool(state.get("correct")),
            "resolved_correct": bool(state.get("resolved_correct")),
            "resolved_credit": float(state.get("resolved_credit") or 0.0),
            "budget_exhausted": bool(state.get("budget_exhausted")),
            "wall_time_ms": state.get("wall_time_ms"),
            "repository_unchanged": state.get("repository_unchanged"),
            **trial_metrics(trial, task),
        }
        rows.append(row)
    by_system = defaultdict(list)
    for row in rows:
        by_system[row["system"]].append(row)
    paired = defaultdict(dict)
    for row in rows:
        paired[(row["task_id"], row["repetition"])][row["system"]] = row
    return {
        "version": "end-to-end-v1",
        "run": str(run),
        "trials": len(rows),
        "systems": {system: summarize(system_rows)
                    for system, system_rows in sorted(by_system.items())},
        "by_question": {
            f"{task_id}#{repetition}": {
                system: {"resolved": row["resolved_correct"],
                         "input": row["tokens"]["input"],
                         "bytes": row["retrieval_bytes"],
                         "calls": row["calls"],
                         "calls_after_hit": row["calls_after_first_hit"],
                         "bytes_after_hit": row["bytes_after_first_hit"]}
                for system, row in cell.items()}
            for (task_id, repetition), cell in sorted(paired.items())},
        "rows": rows,
        "limitations": "Bytes are what each tool returned, before the client's own truncation. "
                       "First hit is a textual appearance of a gold path or symbol in a tool "
                       "result, which proves visibility, not that the model used it. Codex "
                       "reports usage once per turn, so tokens after the first hit are not "
                       "recoverable for that client; calls and bytes after the hit are. "
                       "`context_token_turns` prices a payload by how many later turns must "
                       "carry it, at four bytes per token and assuming no prefix eviction; it "
                       "ranks payloads, it does not reproduce a bill.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"trials": result["trials"], "systems": result["systems"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
