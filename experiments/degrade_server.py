#!/usr/bin/env python3
"""A deliberately worse retrieval server, so a suite can prove it sees retrieval at all.

Every number this project publishes compares HEAD against a shell agent, and nothing has ever
measured what happens when the page is *wrong*. So no suite here has shown it can tell a good page
from a bad one, and five of the seven published studies cannot separate their arms on quality at
all (`quality_separation` in `experiments/published_results.json`). That is the missing positive
control: if a crippled server scores like HEAD, the suite is not measuring retrieval and no result
from it means anything - including the ties.

This is a stdio proxy, not a fork of the server. It speaks JSON-RPC to the client, runs the real
binary underneath, and damages one property of the answer on the way back. `initialize` and
`tools/list` pass through untouched, so the tool surface and the prompt prefix stay byte-identical
to HEAD's and a token comparison against this arm remains honest.

Three knobs, one arm each, because a bundled degradation cannot be attributed:

  --shuffle            Seeded permutation of the result rows. Ranking is destroyed and the bytes
                       are not, so only quality can move; a token difference against HEAD would be
                       a defect in this proxy rather than a finding.
  --page-fraction F    Keep the leading fraction of the rows. This one does move bytes, so it
                       prices page size and quality together and must not be read as a ranking
                       result.
  --blind-attribution  Drop the enclosing-definition identity from every row: `symbol` from a
                       `search_concept` row, `caller` from a `find_callers` row. This is the
                       transport placebo: the client forwards an MCP result whole and truncates
                       shell output to about a tenth of it, so "being forwarded whole" is a live
                       alternative explanation for a measured gap, and this arm is what separates
                       it from "structure helps". It moves bytes too. `search_exact` rows are
                       already path, line and snippet, so the knob is correctly a no-op there.

                       **What is left is a path and a line, and an excerpt only if the agent
                       asked for one.** An earlier draft of this paragraph promised the excerpt
                       unconditionally and that was wrong: `search_concept` returns source text
                       only on `fields: ["excerpt"]`, and its own tool description tells the model
                       to ask only when it needs it. Measured over the archived runs, the share of
                       concept pages carrying an excerpt is 11% on the Claude Django study, 54-58%
                       on the two kernel studies and 100% on etcd, so without `--force-excerpt`
                       this arm removes content as well as structure by an amount set by client
                       behaviour - which confounds the very alternative it exists to separate.

  --force-excerpt      Pin `fields: ["excerpt"]` onto every `search_concept` call on the way in,
                       so the page carries source text whichever way the model asked. Alone it
                       degrades nothing and is a legitimate arm: it is the one-variable baseline
                       the attribution contrast needs, because a blinded arm that forces excerpts
                       compared against a HEAD that does not would differ in two things at once.
                       Blinding is still severe with it on - over the rows that carried a gold
                       identity, 66% lose it even when an excerpt is present, because a
                       `find_callers` excerpt is the call-site line naming the callee rather than
                       the enclosing caller - so one pinned arm can serve as both the positive
                       control and the placebo. `runs/page-position-20260922`.

Only `results` rows are touched. `read_source` returns `lines`, and is left alone: it is how an
agent verifies evidence, and damaging it would confound retrieval with verification.

Known ceiling on --blind-attribution: a `find_callers` payload also carries a
`candidate_definitions` block that names the resolved definition, and this leaves it standing, so
the arm is a *lower* bound on what attribution is worth rather than a clean zero. Emptying that
block means zeroing `candidate_count` and `candidate_definition_count` with it, or the payload
contradicts itself; that is the upgrade path if the lower bound turns out to be too loose.

A column is only dropped where it can be dropped by splitting a row once, from the end that column
sits at: `caller` renders first, `symbol` renders last. A field anywhere else raises rather than
guessing. The renderer does escape a tab inside a value - measured on a tab-indented C excerpt,
which arrives as `\\t` - so splitting on every tab would in fact have worked; this does not depend
on that, which is the point, because nothing tests that escaping on behalf of this proxy.

Failure is loud. A payload this proxy cannot degrade coherently kills the arm rather than passing
through intact, because a control that silently stopped degrading scores like HEAD and reads as
"the suite cannot see retrieval" - the exact false negative it exists to rule out.

    degrade_server.py --server target/release/retrieval-mcp --shuffle -- --root CORPUS
    degrade_server.py --server target/release/retrieval-mcp --force-excerpt -- --root CORPUS
    degrade_server.py --server target/release/retrieval-mcp --blind-attribution --force-excerpt \\
        -- --root CORPUS

No model, no network.
"""
import argparse
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import threading

# `results[12] end_line\tpath\tscore\t...` - the row block's header, followed by exactly that many
# row lines before the next key. Every tool that ranks renders its rows this way.
ROW_HEADER = re.compile(r"^results\[(\d+)\] (.*)$")
# The fields that carry an enclosing-definition identity, which is the property the record says
# pays: "correct evidence reduces recovery turns" came from a caller row starting to tell the
# truth about its call site. `search_exact` rows have neither and are left as they are.
ATTRIBUTION = ("symbol", "caller")
# `fields` belongs to `ConceptArgs` alone, and every argument struct is `deny_unknown_fields`, so
# pinning it onto any other tool's call would have the server reject the request rather than
# answer it richly. One tool, named explicitly, for that reason.
EXCERPT_TOOL = "search_concept"


def drop_column(columns, rows, field):
    """Remove one column, parsing a row only when the column is not at an end.

    `caller` sorts first and `symbol` sorts last, so on those pages one split from the correct end
    is enough and the rest of the row is never interpreted.

    `symbol` is only last while the row has no excerpt. Ask for one and the renderer appends
    `excerpt_truncated` after it - `end_line excerpt path score start_line symbol
    excerpt_truncated` - so the field lands in the middle and the cheap split cannot reach it.
    Refusing that case meant `--blind-attribution` killed the arm on the first excerpt-bearing
    page, which is every page on the etcd study and about half of the kernel's; the proxy had
    never been run against a live server on one. So a middle column is dropped by splitting the
    whole row, which is safe only because the renderer escapes a tab inside a value.

    Nothing tests that escaping on this proxy's behalf, so it is verified rather than trusted: a
    row that does not yield exactly one value per column raises, and the arm dies the same way it
    does for any other payload this cannot degrade coherently.
    """
    index = columns.index(field)
    if index == 0:
        return columns[1:], [row.split("\t", 1)[1] for row in rows]
    if index == len(columns) - 1:
        return columns[:-1], [row.rsplit("\t", 1)[0] for row in rows]
    kept = []
    for row in rows:
        values = row.split("\t")
        if len(values) != len(columns):
            raise ValueError(f"dropping {field!r} at column {index} split a row into "
                             f"{len(values)} values for {len(columns)} columns; a raw tab inside "
                             f"a value would make this drop wrong")
        kept.append("\t".join(values[:index] + values[index + 1:]))
    return columns[:index] + columns[index + 1:], kept


def rewrite_text(text, order, blind):
    """Apply the permutation and the blinding to the rendered rows, keeping the count honest."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        header = ROW_HEADER.match(line)
        if not header:
            continue
        count, columns = int(header.group(1)), header.group(2).split("\t")
        rows = lines[index + 1:index + 1 + count]
        if len(rows) != count or len(order) > count:
            raise ValueError(f"row block claims {count} rows and the text holds {len(rows)}")
        kept = [rows[position] for position in order]
        for field in blind:
            columns, kept = drop_column(columns, kept, field)
        head = f"results[{len(kept)}] " + "\t".join(columns)
        return "\n".join(lines[:index] + [head] + kept + lines[index + 1 + count:])
    raise ValueError("no results block in a payload whose structured content has results")


def degrade(payload, order_for, blind_attribution=False):
    """Damage one tool result in both the representations a client may read.

    Claude Code reads the rendered text and never sees `outputSchema`; Codex forwards the whole
    result. Degrading one and not the other would give the two clients different arms.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        return payload
    structured = result.get("structuredContent")
    if not isinstance(structured, dict) or not isinstance(structured.get("results"), list):
        return payload
    rows = structured["results"]
    if not rows:
        return payload
    order = order_for(len(rows))
    blind = []
    for field in ATTRIBUTION if blind_attribution else ():
        present = sum(field in row for row in rows)
        if present == len(rows):
            blind.append(field)
        elif present:
            raise ValueError(f"{field!r} is in {present} of {len(rows)} rows; the text renders one "
                             f"column per header, so this cannot be dropped consistently")
    contents = result.get("content")
    if not isinstance(contents, list) or not contents:
        raise ValueError("a tool result with rows and no content block")
    for block in contents:
        if block.get("type") == "text":
            block["text"] = rewrite_text(block["text"], order, blind)
            break
    else:
        raise ValueError("a tool result with rows and no text block")
    structured["results"] = [{key: value for key, value in rows[position].items()
                              if key not in blind} for position in order]
    return payload


def pin_excerpt(payload):
    """Add `excerpt` to a `search_concept` call's `fields`, leaving every other request alone.

    This is the only thing in this proxy that touches a request rather than a response. It exists
    so the blinded arm and its baseline differ in exactly one property: without it, the amount of
    *content* a blinded page loses is set by how often the model happened to ask for source text,
    which is client behaviour and not the variable under test.
    """
    if payload.get("method") != "tools/call":
        return payload
    params = payload.get("params")
    if not isinstance(params, dict) or params.get("name") != EXCERPT_TOOL:
        return payload
    arguments = params.setdefault("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError(f"a {EXCERPT_TOOL} call whose arguments are {type(arguments).__name__}")
    fields = arguments.get("fields")
    if fields is None:
        arguments["fields"] = ["excerpt"]
    elif isinstance(fields, list):
        # `fields: []` is a real thing an agent sends, and appending to it is the same pin.
        if "excerpt" not in fields:
            arguments["fields"] = [*fields, "excerpt"]
    else:
        raise ValueError(f"a {EXCERPT_TOOL} call whose fields are {type(fields).__name__}")
    return payload


def rewrite_request(line):
    """One inbound JSON-RPC line with the excerpt pin applied. Non-JSON passes through."""
    try:
        payload = json.loads(line)
    except ValueError:
        return line
    return json.dumps(pin_excerpt(payload), ensure_ascii=False) + "\n"


def orderer(shuffle, fraction, seed):
    """The permutation applied to every ranked page, as a function of its length."""
    rng = random.Random(seed)

    def order_for(count):
        order = list(range(count))
        if shuffle:
            rng.shuffle(order)
        if fraction is not None:
            order = order[:max(1, round(count * fraction))]
        return order
    return order_for


def pump(child, order_for, out, blind_attribution=False):
    """The server's stdout is pure JSON-RPC; its logs go to stderr and are left to the client.

    A payload this cannot degrade takes the whole arm down. Recovering by forwarding it intact
    would leave a control that is HEAD for some calls, and a control that quietly stopped
    degrading scores like HEAD - which reads as "the suite cannot see retrieval", the one
    conclusion this arm exists to make trustworthy.
    """
    for line in child.stdout:
        text = line.strip()
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                out.write(line)
                out.flush()
                continue
            try:
                text = json.dumps(degrade(payload, order_for, blind_attribution),
                                  ensure_ascii=False)
            except ValueError as failure:
                print(f"degrade_server: {failure}; killing the arm rather than answering as HEAD",
                      file=sys.stderr, flush=True)
                os._exit(1)
        out.write(text + "\n")
        out.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", type=Path, required=True, help="the real server binary")
    parser.add_argument("--shuffle", action="store_true", help="destroy ranking, keep the bytes")
    parser.add_argument("--page-fraction", type=float, default=None,
                        help="keep this leading fraction of every page")
    parser.add_argument("--blind-attribution", action="store_true",
                        help="drop the enclosing-definition identity: the transport placebo")
    parser.add_argument("--force-excerpt", action="store_true",
                        help="pin fields:[\"excerpt\"] on every search_concept call on the way in")
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="-- followed by the real server's own arguments")
    args = parser.parse_args()
    # `--force-excerpt` alone degrades nothing and is still not HEAD: it is the pinned baseline a
    # blinded arm has to be compared against, or the two arms differ in two variables at once.
    if (not args.shuffle and args.page_fraction is None and not args.blind_attribution
            and not args.force_excerpt):
        parser.error("a control arm that neither degrades nor pins anything is HEAD; pass "
                     "--shuffle, --page-fraction, --blind-attribution or --force-excerpt")
    if args.page_fraction is not None and not 0 < args.page_fraction < 1:
        parser.error("--page-fraction must lie strictly between 0 and 1")
    server_args = args.rest[1:] if args.rest[:1] == ["--"] else args.rest
    child = subprocess.Popen([str(args.server), *server_args], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1)
    order_for = orderer(args.shuffle, args.page_fraction, args.seed)
    reader = threading.Thread(target=pump,
                              args=(child, order_for, sys.stdout, args.blind_attribution),
                              daemon=True)
    reader.start()
    try:
        for line in sys.stdin:
            if args.force_excerpt:
                try:
                    line = rewrite_request(line)
                except ValueError as failure:
                    # Same policy as `pump`: an arm that quietly stopped pinning is an arm that
                    # differs from its baseline in a second, unrecorded variable.
                    print(f"degrade_server: {failure}; killing the arm rather than passing the "
                          f"call through unpinned", file=sys.stderr, flush=True)
                    os._exit(1)
            child.stdin.write(line)
            child.stdin.flush()
    except BrokenPipeError:
        pass
    child.stdin.close()
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
