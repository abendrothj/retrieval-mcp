#!/usr/bin/env python3
"""Cut fresh scoped corpora from the upstream checkouts in `corpora/`. No model calls.

A suite must never be authored against a corpus an earlier suite already spent, so this cuts
scopes disjoint from the ones behind every frozen question set: the Django held-out scope
(db, core, utils, dispatch, apps), the VS Code platform corpus, and the coreutils files the
authored suites already ask about. Output is a directory per corpus plus a manifest holding the
same fingerprint `comparison_runner` computes, so a later run can prove it used these bytes.
"""
import argparse
import json
from pathlib import Path
import shutil

import comparison_runner

# Each scope is (include roots, excluded path fragments, kept suffixes).
SCOPES = {
    "dj-forms": {
        "source": "corpora/django-5.1.4",
        "include": ["django/forms", "django/template", "django/templatetags", "django/views",
                    "django/http", "django/urls", "django/middleware", "django/conf"],
        "exclude": ["/locale/", "/conf/app_template/", "/conf/project_template/"],
        "suffixes": [".py"],
        "note": "Disjoint from the Django held-out scope (db, core, utils, dispatch, apps).",
    },
    "vs-editor": {
        "source": "corpora/vscode-1.96.0",
        "include": ["src/vs/editor/common", "src/vs/editor/browser"],
        "exclude": ["/test/"],
        "suffixes": [".ts"],
        "note": "Disjoint from the VS Code platform corpus used by the authored and mechanical suites.",
    },
    "cu-text": {
        "source": "corpora/coreutils",
        "include": ["src/uu/sort", "src/uu/ls", "src/uu/cut", "src/uu/join", "src/uu/uniq",
                    "src/uu/pr", "src/uu/printf", "src/uu/stat", "src/uu/tail", "src/uu/head",
                    "src/uu/wc", "src/uu/expr", "src/uu/date", "src/uu/seq", "src/uu/tr",
                    "src/uu/paste", "src/uu/nl", "src/uu/shuf", "src/uu/tsort", "src/uu/truncate",
                    "src/uucore/src/lib/features", "src/uucore/src/lib/lib.rs"],
        # The authored coreutils suites already ask about these files; keep them out so no
        # question can be recycled knowledge.
        "exclude": ["diagnostics", "hardware.rs"],
        "suffixes": [".rs"],
        "note": "Coreutils text utilities plus shared uucore features, minus the files the "
                "authored coreutils suites already ask about.",
    },
}


def files_for(source, scope):
    kept = []
    for root in scope["include"]:
        base = source / root
        if not base.exists():
            raise FileNotFoundError(f"missing scope root: {base}")
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix not in scope["suffixes"]:
                continue
            relative = path.relative_to(source).as_posix()
            if any(fragment.strip("/") in relative for fragment in scope["exclude"]):
                continue
            kept.append(relative)
    return sorted(set(kept))


def cut(repository, output, names):
    manifest = {"version": "cut-corpora-v1", "corpora": {}}
    for name in names:
        scope = SCOPES[name]
        source = (repository / scope["source"]).resolve(strict=True)
        destination = output / name
        if destination.exists():
            shutil.rmtree(destination)
        kept = files_for(source, scope)
        for relative in kept:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
        manifest["corpora"][name] = {
            "source": scope["source"],
            "include": scope["include"],
            "exclude": scope["exclude"],
            "note": scope["note"],
            "files": len(kept),
            **comparison_runner.source_fingerprint(destination),
        }
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpora", nargs="+", default=sorted(SCOPES), choices=sorted(SCOPES))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.output = args.output.resolve(strict=True)
    manifest = cut(args.repository, args.output, args.corpora)
    (args.output / "corpora-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
