#!/usr/bin/env python3
"""Scan a repo directory and output structural facts.

Produces repo-profile.yaml containing:
  - stats: file counts, language breakdown, notable dirs
  - directory_tree: 2-level deep source file counts per directory
  - entry_exports: public symbols from entry files (AST-extracted)

All output is purely factual — no domain suggestions, no source root
detection, no semantic interpretation. Those are the Researcher's job.

Usage:
    python repo_manifest.py <workspace> [--repo <repo-path>]

Output: <workspace>/knowledge/repo-profile.yaml
Stdout: 3-line summary.
"""
import sys
import os
import re
import ast
from pathlib import Path
from collections import Counter

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

LANG_EXTENSIONS = {
    "Python": {".py"},
    "TypeScript": {".ts", ".tsx"},
    "JavaScript": {".js", ".jsx", ".mjs"},
    "Go": {".go"},
    "Rust": {".rs"},
    "Java": {".java"},
    "Ruby": {".rb"},
}

ALL_SOURCE_EXTS = {ext for exts in LANG_EXTENSIONS.values() for ext in exts}

SKIP_DIRS = {
    "node_modules", "dist", "build", ".build", "vendor", "__pycache__",
    ".git", ".svn", ".hg", "venv", ".venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "egg-info", ".eggs", "target", "out", "bin", "obj",
    "coverage", ".coverage", ".nyc_output", ".next",
}

SKIP_FILE_RE = re.compile(
    r"(test[s_]?[/\\]|__tests__|\.test\.|\.spec\.|_test\.py$|"
    r"benchmark[s]?[/\\]|fixture[s]?[/\\]|conftest\.py$)",
    re.IGNORECASE,
)

ENTRY_RE = re.compile(
    r"^(index|main|mod|lib|app|__init__|server|client)\.",
    re.IGNORECASE,
)

EXAMPLE_NAMES = {"examples", "example", "cookbooks", "cookbook", "demos", "demo", "samples", "sample"}
DOC_NAMES = {"docs", "doc", "documentation"}


def scan_repo(repo_path):
    source_files = []
    ext_counts = Counter()
    total_files = 0

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel_root = os.path.relpath(root, repo_path)

        for f in files:
            total_files += 1
            rel = os.path.join(rel_root, f) if rel_root != "." else f
            p = Path(f)
            if p.suffix in ALL_SOURCE_EXTS and not SKIP_FILE_RE.search(rel):
                source_files.append(rel)
                ext_counts[p.suffix] += 1

    return source_files, total_files, ext_counts


def detect_language(ext_counts):
    lang_counts = Counter()
    for ext, count in ext_counts.items():
        for lang, exts in LANG_EXTENSIONS.items():
            if ext in exts:
                lang_counts[lang] += count
    return lang_counts.most_common(1)[0][0] if lang_counts else "Unknown"


def find_notable_dirs(repo_path):
    notable = []
    has_docs = has_examples = False
    for item in sorted(os.listdir(repo_path)):
        full = os.path.join(repo_path, item)
        if not os.path.isdir(full) or item.startswith(".") or item in SKIP_DIRS:
            continue
        if item.lower() in DOC_NAMES:
            has_docs = True
        if item.lower() in EXAMPLE_NAMES:
            has_examples = True
        notable.append(item + "/")
    return notable, has_docs, has_examples


def build_directory_tree(repo_path, source_files):
    """Build a 2-level deep directory tree with file counts and markers."""
    tree = {}
    for f in source_files:
        parts = Path(f).parts
        if len(parts) >= 1:
            top = parts[0]
            if top not in tree:
                tree[top] = {"_count": 0, "_has_init": False}
            tree[top]["_count"] += 1

            if len(parts) >= 2 and os.path.isdir(os.path.join(repo_path, top)):
                sub = parts[1] if len(parts) > 2 else None
                if sub and sub not in tree[top]:
                    tree[top][sub] = 0
                if sub:
                    tree[top][sub] += 1

    for top in tree:
        init_candidates = ("__init__.py", "index.ts", "index.js", "mod.rs", "lib.rs")
        top_path = os.path.join(repo_path, top)
        if os.path.isdir(top_path):
            tree[top]["_has_init"] = any(
                os.path.isfile(os.path.join(top_path, f)) for f in init_candidates
            )

    result = {}
    for top in sorted(tree, key=lambda t: -tree[t]["_count"]):
        info = tree[top]
        count = info["_count"]
        has_init = info["_has_init"]
        marker = f"{count} files" + (", package root" if has_init else "")

        subs = {k: v for k, v in info.items() if not k.startswith("_") and isinstance(v, int) and v > 0}
        if subs:
            sub_entries = {f"{k}/": f"{v} files" for k, v in sorted(subs.items(), key=lambda x: -x[1])}
            result[f"{top}/ # {marker}"] = sub_entries
        else:
            result[f"{top}/ # {marker}"] = None

    return result


def extract_entry_exports(repo_path, source_files):
    entries = {}
    for rel in source_files:
        if not ENTRY_RE.match(os.path.basename(rel)):
            continue
        full = os.path.join(repo_path, rel)
        try:
            with open(full, errors="replace") as f:
                content = f.read(8192)
        except OSError:
            continue

        exports = _extract_symbols(content, Path(rel).suffix)
        if exports:
            entries[rel] = exports[:15]
    return entries


def _extract_symbols(content, ext):
    if ext == ".py":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return _regex_symbols(content)
        symbols = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                symbols.append(f"class {node.name}")
            elif isinstance(node, ast.FunctionDef):
                symbols.append(f"def {node.name}()")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        symbols.append(f"{t.id} = ...")
        return symbols
    return _regex_symbols(content)


def _regex_symbols(content):
    symbols = []
    for line in content.split("\n")[:100]:
        line = line.strip()
        m = re.match(
            r"export\s+(?:default\s+)?(function|class|const|type|interface|enum)\s+(\w+)", line
        )
        if m:
            symbols.append(f"{m.group(1)} {m.group(2)}")
            continue
        m = re.match(r"func\s+(\w+)\s*\(", line)
        if m:
            symbols.append(f"func {m.group(1)}()")
            continue
        m = re.match(r"pub\s+(fn|struct|enum|trait|type)\s+(\w+)", line)
        if m:
            symbols.append(f"pub {m.group(1)} {m.group(2)}")
    return symbols


def discover_repo(workspace):
    repo_dir = os.path.join(workspace, "repo")
    if not os.path.isdir(repo_dir):
        return None
    entries = [e for e in os.listdir(repo_dir) if os.path.isdir(os.path.join(repo_dir, e)) and not e.startswith(".")]
    if len(entries) == 1:
        return os.path.join(repo_dir, entries[0])
    src_files = [e for e in os.listdir(repo_dir) if os.path.isfile(os.path.join(repo_dir, e))]
    if src_files:
        return repo_dir
    if entries:
        return os.path.join(repo_dir, entries[0])
    return repo_dir


def main():
    if len(sys.argv) < 2:
        print("Usage: python repo_manifest.py <workspace> [--repo <repo-path>]", file=sys.stderr)
        sys.exit(1)

    workspace = sys.argv[1]
    repo_path = None
    if "--repo" in sys.argv:
        idx = sys.argv.index("--repo")
        if idx + 1 < len(sys.argv):
            repo_path = sys.argv[idx + 1]

    if not repo_path:
        repo_path = discover_repo(workspace)
    if not repo_path or not os.path.isdir(repo_path):
        print(f"Error: cannot find repo under {workspace}/repo/", file=sys.stderr)
        sys.exit(1)

    source_files, total_files, ext_counts = scan_repo(repo_path)
    notable_dirs, has_docs, has_examples = find_notable_dirs(repo_path)
    entry_exports = extract_entry_exports(repo_path, source_files)
    primary_lang = detect_language(ext_counts)

    lang_breakdown = {}
    for ext, count in ext_counts.most_common():
        lang = detect_language({ext: count})
        lang_breakdown[lang] = lang_breakdown.get(lang, 0) + count

    directory_tree = build_directory_tree(repo_path, source_files)

    profile = {
        "stats": {
            "total_source_files": len(source_files),
            "total_files": total_files,
            "primary_language": primary_lang,
            "has_readme": os.path.isfile(os.path.join(repo_path, "README.md")),
            "has_docs": has_docs,
            "has_examples": has_examples,
            "notable_dirs": notable_dirs,
            "language_breakdown": lang_breakdown,
        },
        "directory_tree": directory_tree,
        "entry_exports": entry_exports if entry_exports else None,
    }

    out_path = os.path.join(workspace, "knowledge", "repo-profile.yaml")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(profile, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"total_source_files={len(source_files)} primary_language={primary_lang}")
    print(f"has_docs={has_docs} has_examples={has_examples}")
    print(f"output={out_path}")


if __name__ == "__main__":
    main()
