#!/usr/bin/env python3
"""Build a shared semantic cache once, outside any trial. Runs the real backend; no model calls."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import benchmark


def warm(server, root, cache, semantic_command, timeout):
    server, root, cache = server.resolve(strict=True), root.resolve(strict=True), cache.resolve()
    if not cache.is_absolute():
        raise ValueError("cache directory must be absolute")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache.is_relative_to(root):
        raise ValueError("keep the cache outside the corpus")
    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+") as stderr:
        client = benchmark.MCP([str(server), "--root", str(root), "--profile", "C",
                                "--ranker", "semantic",
                                "--semantic-command", json.dumps(semantic_command),
                                "--timeout-seconds", str(timeout)],
                               dict(os.environ, RETRIEVAL_SEMANTIC_CACHE_DIR=str(cache)),
                               root, stderr, timeout + 60)
        try:
            client.request("initialize", {"protocolVersion":"2025-11-25", "capabilities":{},
                "clientInfo":{"name":"semantic-warmup", "version":"1"}})
            client.send({"jsonrpc":"2.0", "method":"notifications/initialized"})
            result = client.request("tools/call", {"name":"search_concept",
                                                   "arguments":{"query":"index warmup", "limit":1}})
            if result.get("isError"):
                raise RuntimeError(f"warmup failed: {result['content'][0]['text']}")
        finally:
            client.close()
    files = sorted(p.name for p in cache.iterdir() if p.is_file())
    return {"root":str(root), "cache":str(cache), "server":str(server),
            "build_profile":server.parent.name, "seconds":round(time.monotonic()-started, 1),
            "cache_bytes":sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()),
            "cache_files":files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parent
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--server", type=Path, default=base.parent/"target/release/retrieval-mcp")
    parser.add_argument("--semantic-command", type=benchmark.command_array, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    print(json.dumps(warm(args.server, args.root, args.cache, args.semantic_command, args.timeout)))


if __name__ == "__main__":
    sys.exit(main())
