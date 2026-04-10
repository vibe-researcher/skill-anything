#!/usr/bin/env python3
"""Extract API surface (class/function signatures + docstring first lines) from source files.

Usage:
    python extract_api_surface.py <file1> [<file2> ...] [--output <path>]

Outputs compact YAML with per-file signatures.  Designed to run inside
a workspace so Researcher agents can read the output instead of opening
every file individually.
"""
import sys
import os
import re
import ast
import textwrap
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def extract_python(filepath):
    """Use AST to extract classes, functions, and their docstrings."""
    try:
        with open(filepath, errors="replace") as f:
            source = f.read()
        tree = ast.parse(source)
    except (SyntaxError, OSError):
        return None

    line_count = source.count("\n") + 1
    symbols = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            entry = {"type": "class", "name": node.name, "line": node.lineno}
            doc = ast.get_docstring(node)
            if doc:
                entry["doc"] = doc.split("\n")[0][:120]
            methods = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("_") and child.name != "__init__":
                        continue
                    sig = _func_sig(child)
                    methods.append(sig)
            if methods:
                entry["methods"] = methods[:10]
            symbols.append(entry)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            entry = _func_sig(node)
            doc = ast.get_docstring(node)
            if doc:
                entry["doc"] = doc.split("\n")[0][:120]
            symbols.append(entry)

    return {"lines": line_count, "symbols": symbols} if symbols else {"lines": line_count, "symbols": []}


def _func_sig(node):
    args = []
    for arg in node.args.args:
        if arg.arg == "self":
            continue
        hint = ""
        if arg.annotation:
            hint = f": {ast.unparse(arg.annotation)}" if hasattr(ast, "unparse") else ""
        args.append(f"{arg.arg}{hint}")

    ret = ""
    if node.returns and hasattr(ast, "unparse"):
        ret = f" -> {ast.unparse(node.returns)}"

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return {"type": "function", "name": node.name, "sig": f"{prefix} {node.name}({', '.join(args)}){ret}", "line": node.lineno}


def extract_generic(filepath):
    """Regex-based extraction for non-Python files."""
    try:
        with open(filepath, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    symbols = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # TypeScript / JavaScript
        m = re.match(
            r"export\s+(?:default\s+)?(?:abstract\s+)?(function|class|const|type|interface|enum)\s+(\w+)",
            stripped,
        )
        if m:
            symbols.append({"type": m.group(1), "name": m.group(2), "line": i})
            continue
        # Go
        m = re.match(r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", stripped)
        if m:
            symbols.append({"type": "func", "name": m.group(1), "line": i})
            continue
        # Rust
        m = re.match(r"pub\s+(fn|struct|enum|trait|type|mod)\s+(\w+)", stripped)
        if m:
            symbols.append({"type": m.group(1), "name": m.group(2), "line": i})

    return {"lines": len(lines), "symbols": symbols}


def extract_file(filepath):
    ext = Path(filepath).suffix
    if ext == ".py":
        return extract_python(filepath)
    return extract_generic(filepath)


def main():
    files = []
    output_path = None
    args = sys.argv[1:]

    i = 0
    while i < len(args):
        if args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        else:
            files.append(args[i])
            i += 1

    if not files:
        print("Usage: python extract_api_surface.py <file1> [<file2> ...] [--output <path>]", file=sys.stderr)
        sys.exit(1)

    result = {}
    for filepath in files:
        if not os.path.isfile(filepath):
            result[filepath] = {"error": "file not found"}
            continue
        data = extract_file(filepath)
        if data is not None:
            result[filepath] = data

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            yaml.dump(result, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"Extracted {len(result)} files -> {output_path}")
    else:
        yaml.dump(result, sys.stdout, allow_unicode=True, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
