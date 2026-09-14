#!/usr/bin/env python3
"""Assert that the documentation still describes the server that exists.

Every claim in this project's READMEs is meant to be downstream of a measurement, but nothing was
checking that the prose kept up with the code. It did not: freezing the default surface at four
tools left the README quickstart calling a tool the default no longer exposes, so the first command
a reader runs returned `unknown or disabled tool`. The same drift left three different test counts,
a profile table marking the wrong row as the default, and a tool documented under a name it had
been renamed away from.

These checks need no model and no network:

  Tool surface  - the tools each document names as the default, as the full catalogue, and in the
                  profile table match `TOOLS`, `DEFAULT_SURFACE` and `PROFILES` in src/config.
  Test counts   - the counts written beside the documented commands match what those commands
                  actually report.
  Paths         - every repository-relative path a document points at exists.
  Links         - every cross-document link resolves, and every `#anchor` names a real heading.
  Quickstart    - the quickstart block runs against this build and returns a result, not an error.

Exit status is nonzero when any check fails, so this runs as a build step.

The top-level README of the surrounding study lives outside this git repository, so it is checked
only when it is present on disk; CI covers the three tracked documents.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

TRACKED_DOCS = ("README.md", "experiments/README.md", "experiments/FACTOR_PROTOCOL.md",
                "experiments/CORPUS_V2.md")
OUTSIDE_DOC = "../README.md"
# A path-shaped token in prose is only checked when it looks like this repository's own files.
PATH_PATTERN = re.compile(r'`((?:experiments|src|tests|examples)/[A-Za-z0-9_./*-]+)`')
PLACEHOLDER = re.compile(r'\*|\{|/path/to/|/absolute/')
LINK_PATTERN = re.compile(r'\[[^\]]*\]\(([^)\s#]*)(#[A-Za-z0-9-]+)?\)')


def headings(text):
    """GitHub's heading slugs, for checking `#anchor` link targets."""
    return {re.sub(r'[^a-z0-9 -]', '', line.lower()).replace(' ', '-')
            for line in re.findall(r'^#{1,6} (.+)$', text, re.M)}


def rust_constants(repo):
    """Parse TOOLS, DEFAULT_SURFACE and PROFILES out of src/config/mod.rs."""
    source = (repo / "src/config/mod.rs").read_text(encoding="utf-8")

    def names(body):
        return [name for name in re.findall(r'"([a-z_]+)"', body)]

    tools = names(re.search(r'pub const TOOLS[^=]*=\s*\[(.*?)\];', source, re.S).group(1))
    default = names(re.search(r'pub const DEFAULT_SURFACE[^=]*=\s*\[(.*?)\];', source, re.S).group(1))
    block = re.search(r'pub const PROFILES[^=]*=\s*\[(.*?)\n\];', source, re.S).group(1)
    profiles = {}
    for letter, body in re.findall(r'\("([A-D])",\s*&(\[.*?\]|TOOLS)', block, re.S):
        profiles[letter] = list(tools) if body == "TOOLS" else names(body)
    return tools, default, profiles


def test_counts(repo):
    """Run the documented test commands and report what they actually count."""
    targets = {"library": ["--lib"], "stdio": ["--test", "mcp_stdio"], "example": ["--examples"]}
    counts = {}
    for binary, flags in targets.items():
        rust = subprocess.run(["cargo", "test", "--locked", *flags], cwd=repo,
                              capture_output=True, text=True)
        if rust.returncode != 0:
            raise SystemExit(f"cargo test {' '.join(flags)} failed; fix the tests before the docs\n"
                             + rust.stdout[-2000:])
        passed = ignored = 0
        for line in rust.stdout.splitlines():
            result = re.search(r'test result: ok\. (\d+) passed; \d+ failed; (\d+) ignored', line)
            if result:
                passed += int(result.group(1)) + int(result.group(2))
                ignored += int(result.group(2))
        counts[binary] = (passed, ignored)
    python = subprocess.run(
        ["python3", "-W", "error::ResourceWarning", "-m", "unittest", "discover",
         "-s", "experiments", "-p", "test_*.py"], cwd=repo, capture_output=True, text=True)
    if python.returncode != 0:
        raise SystemExit("python tests failed; fix the tests before the docs\n"
                         + python.stderr[-2000:])
    ran = re.search(r'Ran (\d+) tests', python.stderr)
    return counts, int(ran.group(1))


def check_surface(name, text, tools, default, profiles, problems):
    def report(detail):
        problems.append({"document": name, "check": "tool surface", "detail": detail})

    for quoted, documented in re.findall(
            r'(defaults to the four[^:]*:|exposes\s*\n?)((?:\s*`[a-z_]+`[,\s]*(?:and\s*)?)+)', text):
        named = re.findall(r'`([a-z_]+)`', documented)
        if sorted(named) != sorted(default):
            report(f"documented default surface {named} != DEFAULT_SURFACE {default}")

    profile_table = re.search(
        r'^\|\s*Profile\s*\|[^\n]*--tools[^\n]*\|\n\|[-\s|]+\|\n((?:\|.*\n)+)', text, re.M)
    for letter, documented in re.findall(r'^\|\s*([A-D])\s*\|\s*(.+?)\s*\|',
                                         profile_table.group(1) if profile_table else "", re.M):
        if letter not in profiles:
            continue
        named = re.findall(r'[a-z_]+', documented.replace("`", ""))
        expected = profiles[letter]
        if named == ["all", "seven"]:
            named = expected if len(expected) == 7 else ["all", "seven"]
        if named != expected:
            report(f"profile {letter} documented as {named} != PROFILES[{letter}] {expected}")

    for stale in re.findall(r'`(search_semantic|find_definition)`', text):
        if "renamed" not in text[max(0, text.find(stale) - 200):text.find(stale) + 200]:
            report(f"`{stale}` is not a tool this server exposes; the catalogue is {tools}")

    documented_count = re.search(r'(?:can expose|The server exposes) (\w+) tools', text)
    words = {"four": 4, "five": 5, "six": 6, "seven": 7}
    if documented_count and words.get(documented_count.group(1)) != len(tools):
        report(f"documented catalogue size '{documented_count.group(1)}' != {len(tools)}")


def check_counts(name, text, rust, python, problems):
    def report(detail):
        problems.append({"document": name, "check": "test counts", "detail": detail})

    for library, stdio, ignored, example in re.findall(
            r'# (\d+) library, (\d+) stdio \((\d+) ignored\), (\d+) example', text):
        documented = {"library": (int(library), 0), "stdio": (int(stdio), int(ignored)),
                      "example": (int(example), 0)}
        if documented != rust:
            report(f"documented rust counts {documented} != actual {rust}")

    for count in re.findall(r"test_\*\.py'\s*#\s*(\d+) tests", text):
        if int(count) != python:
            report(f"documented python test count {count} != actual {python}")

    for py, rs in re.findall(r'\((\d+) Python tests, (\d+) Rust tests\)', text):
        total = sum(passed for passed, _ in rust.values())
        if int(py) != python or int(rs) != total:
            report(f"documented ({py} Python, {rs} Rust) != actual ({python} Python, {total} Rust)")


def check_paths(name, text, repo, problems):
    for path in sorted(set(PATH_PATTERN.findall(text))):
        if PLACEHOLDER.search(path) or (repo / path).exists():
            continue
        if not (repo / path).parent.is_dir():
            continue
        problems.append({"document": name, "check": "paths",
                         "detail": f"`{path}` does not exist"})


def check_links(name, text, repo, problems):
    document = (repo / name)
    for target, anchor in LINK_PATTERN.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        destination = document.parent / target if target else document
        if not destination.exists():
            problems.append({"document": name, "check": "links",
                             "detail": f"link target {target!r} does not exist"})
            continue
        if anchor and destination.suffix == ".md":
            slugs = headings(destination.read_text(encoding="utf-8"))
            if anchor[1:] not in slugs:
                problems.append({
                    "document": name, "check": "links",
                    "detail": f"{target or name}{anchor} names no heading in that document"})


def check_quickstart(repo, problems):
    """Run the quickstart exactly as written, so it cannot rot again unnoticed."""
    text = (repo / "README.md").read_text(encoding="utf-8")
    block = re.search(r'## Quickstart\n+```sh\n(.*?)```', text, re.S)
    if not block:
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": "no quickstart block found"})
        return
    binary = repo / "target/release/retrieval-mcp"
    if not binary.exists():
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": f"{binary} is not built; run the quickstart's cargo build first"})
        return
    script = block.group(1).replace("cargo build", ": skip cargo build", 1)
    # The quickstart calls `retrieval-mcp` by name, as a reader with `cargo install` would. Run it
    # with this build first on PATH: otherwise the check asks whatever binary happens to be
    # installed, which passed on a developer machine holding a two-day-old `~/.cargo/bin` copy
    # while CI, having none, failed for seven consecutive pushes.
    environment = {**os.environ, "PATH": f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    completed = subprocess.run(["sh", "-c", script], cwd=repo, capture_output=True, text=True,
                               env=environment)
    line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
    try:
        result = json.loads(line)["result"]
    except (ValueError, KeyError, IndexError):
        problems.append({"document": "README.md", "check": "quickstart",
                         "detail": f"quickstart produced no JSON-RPC result: {line[:200]!r}"})
        return
    if result.get("isError"):
        problems.append({
            "document": "README.md", "check": "quickstart",
            "detail": "quickstart returned isError: "
                      f"{result.get('structuredContent', {}).get('error', '')!r}"})


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the checks that must run the test suites to learn the truth")
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)

    tools, default, profiles = rust_constants(repo)
    rust, python = (None, None) if args.skip_tests else test_counts(repo)

    documents = [name for name in TRACKED_DOCS if (repo / name).exists()]
    outside = (repo / OUTSIDE_DOC).resolve()
    if outside.exists():
        documents.append(OUTSIDE_DOC)

    problems = []
    for name in documents:
        text = (repo / name).read_text(encoding="utf-8")
        check_surface(name, text, tools, default, profiles, problems)
        check_paths(name, text, repo, problems)
        check_links(name, text, repo, problems)
        if not args.skip_tests:
            check_counts(name, text, rust, python, problems)
    if not args.skip_tests:
        check_quickstart(repo, problems)

    print(json.dumps({"documents": documents, "checked_outside_repo": OUTSIDE_DOC in documents,
                      "tools": len(tools), "default_surface": default,
                      "rust_tests": rust, "python_tests": python,
                      "problems": len(problems)}, indent=2))
    for problem in problems:
        print(f"\n{problem['document']}\n  - [{problem['check']}] {problem['detail']}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
