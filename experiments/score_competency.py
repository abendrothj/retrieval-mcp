"""Deterministic scoring for synthetic syntax probes; no model or corpus execution."""
from benchmark import grade_answer


def score(task, answer, calls):
    """Calls are {name, arguments, result}; use actual delivered MCP results."""
    scored = grade_answer(task, answer)
    constraint = True
    name = task["id"]
    expected = task["expected_json"]["answer"]
    def data(call):
        return call["result"].get("structuredContent", {}) if not call["result"].get("isError") else {}
    def lines(call):
        return sorted(hit["line"] for hit in data(call).get("results", []) if hit.get("path") == "tokens.py")
    searches = [c for c in calls if c["name"] == "search_exact"]
    if name in ("regex-alternation", "regex-literal-pipe"):
        constraint = any(c["arguments"].get("regex") is True and lines(c) == expected for c in searches)
    elif name == "paginate-entries":
        constraint = (bool(calls) and len(searches) == len(calls)
            and all(type(c["arguments"].get("limit", 20)) is int and 1 <= c["arguments"].get("limit", 20) <= 2 for c in searches)
            and sorted({line for c in searches for line in lines(c)}) == expected)
    elif name == "bounded-read":
        constraint = (bool(calls) and all(c["name"] == "read_source" and c["arguments"].get("path") == "tokens.py"
            and c["arguments"].get("start_line") == c["arguments"].get("end_line") == 9
            and [line["line"] for line in data(c).get("lines", [])] == [9] for c in calls))
    return {"grading":"competency-strict-v1", "correct":bool(scored["correct"] and scored["format_correct"] and constraint),
            "format_correct":scored["format_correct"], "payload_matches":scored["correct"],
            "behavior_constraint_satisfied":constraint,
            "evidence_support":"not_automatically_adjudicated"}
