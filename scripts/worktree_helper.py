#!/usr/bin/env python3
"""Create and manage physically isolated work directories for sub-agents.

Used by Orchestrator (or PreToolUse hook) before spawning a sub-agent that
must NOT see certain parts of the workspace (e.g. Eval Designer must not see
skills/; without_skill Runner must not see skills/).

The isolation directory is created under workspace/.worktrees/<purpose>-<uuid>
and a manifest file (.isolation.json) records what was copied in and what was
explicitly excluded. The Runner/agent is then told its CWD is this directory.

Subcommands:
    create     Create an isolation dir for a given purpose
    list       List active isolation dirs
    remove     Remove a specific isolation dir
    cleanup    Remove isolation dirs older than N days (default 7)

Usage examples:
    # For a Runner with access to repo + skills
    python scripts/worktree_helper.py create --workspace <ws> \\
        --purpose runner-with-iter5-task3 \\
        --include repo,skills

    # For a Runner without skills
    python scripts/worktree_helper.py create --workspace <ws> \\
        --purpose runner-without-iter5-task3 \\
        --include repo --exclude-guard skills

    # For Eval Designer (no repo needed, no skills)
    python scripts/worktree_helper.py create --workspace <ws> \\
        --purpose eval-designer-iter0 \\
        --include knowledge --exclude-guard skills,evals

Output: JSON with the created path and manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _cp_tree(src: Path, dst: Path) -> dict:
    """Copy src -> dst recursively. Uses cp -r for speed on macOS/Linux.

    Returns manifest entry describing what was copied.
    """
    if not src.exists():
        return {"source": str(src), "status": "missing", "files": 0}
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Prefer system cp for symlink-preserving + hardlink-aware behavior
    subprocess.run(["cp", "-R", str(src), str(dst)], check=True)
    # Count files
    count = sum(1 for _ in dst.rglob("*") if _.is_file())
    return {"source": str(src), "dest": str(dst), "status": "copied", "files": count}


def _worktrees_root(ws: Path) -> Path:
    return ws / ".worktrees"


def _ensure_gitignored(ws: Path) -> None:
    """Ensure .worktrees is git-ignored in workspace/.gitignore."""
    gi = ws / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if ".worktrees" not in existing:
        with gi.open("a") as f:
            f.write("\n.worktrees/\n")


def cmd_create(args) -> dict:
    ws = Path(args.workspace).resolve()
    if not ws.exists():
        raise SystemExit(f"workspace does not exist: {ws}")

    includes = [x.strip() for x in args.include.split(",") if x.strip()]
    exclude_guards = [x.strip() for x in (args.exclude_guard or "").split(",") if x.strip()]

    # Validate: none of excluded guards may appear in includes
    conflict = set(includes) & set(exclude_guards)
    if conflict:
        raise SystemExit(
            f"cannot include and exclude-guard the same dirs: {conflict}"
        )

    root = _worktrees_root(ws)
    root.mkdir(parents=True, exist_ok=True)
    _ensure_gitignored(ws)

    dir_name = f"{args.purpose}-{uuid.uuid4().hex[:8]}"
    work_path = root / dir_name
    work_path.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "purpose": args.purpose,
        "work_path": str(work_path),
        "created_at": _now_iso(),
        "includes": [],
        "exclude_guards": exclude_guards,
        "session_id": str(uuid.uuid4()),
    }

    # Copy each included dir
    for name in includes:
        src = ws / name
        dst = work_path / name
        entry = _cp_tree(src, dst)
        manifest["includes"].append(entry)

    # Post-validation: ensure exclude_guards are truly absent
    leaks = []
    for guard in exclude_guards:
        if (work_path / guard).exists():
            leaks.append(guard)
    if leaks:
        # Clean up; this is fatal
        shutil.rmtree(work_path, ignore_errors=True)
        raise SystemExit(f"exclude_guard leak: {leaks} present in work_path")

    # Write the manifest
    manifest_path = work_path / ".isolation.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False)
    )

    return {
        "ok": True,
        "work_path": str(work_path),
        "manifest_path": str(manifest_path),
        "files_total": sum(e.get("files", 0) for e in manifest["includes"]),
    }


def cmd_list(args) -> dict:
    ws = Path(args.workspace).resolve()
    root = _worktrees_root(ws)
    if not root.exists():
        return {"ok": True, "worktrees": []}
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        mp = child / ".isolation.json"
        if mp.exists():
            try:
                m = json.loads(mp.read_text())
                out.append({
                    "work_path": str(child),
                    "purpose": m.get("purpose"),
                    "created_at": m.get("created_at"),
                })
            except json.JSONDecodeError:
                out.append({"work_path": str(child), "purpose": "?",
                            "created_at": "?"})
        else:
            out.append({"work_path": str(child), "purpose": "untracked",
                        "created_at": "?"})
    return {"ok": True, "worktrees": out}


def cmd_remove(args) -> dict:
    path = Path(args.path).resolve()
    ws = Path(args.workspace).resolve()
    # Safety: only allow removal under workspace/.worktrees
    if _worktrees_root(ws) not in path.parents:
        raise SystemExit(
            f"refusing to remove {path}: not under {_worktrees_root(ws)}"
        )
    if not path.exists():
        return {"ok": True, "removed": False, "reason": "not_found"}
    shutil.rmtree(path)
    return {"ok": True, "removed": True, "path": str(path)}


def cmd_cleanup(args) -> dict:
    ws = Path(args.workspace).resolve()
    root = _worktrees_root(ws)
    if not root.exists():
        return {"ok": True, "removed": []}
    cutoff = time.time() - args.age_days * 86400
    removed = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        mtime = child.stat().st_mtime
        if mtime < cutoff:
            shutil.rmtree(child)
            removed.append(str(child))
    return {"ok": True, "removed": removed}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="worktree_helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("create")
    pc.add_argument("--workspace", required=True)
    pc.add_argument("--purpose", required=True,
                    help="Human-readable purpose tag (e.g. runner-with-iter5-t3)")
    pc.add_argument("--include", required=True,
                    help="Comma-separated list of workspace subdirs to copy in")
    pc.add_argument("--exclude-guard", default="",
                    help="Comma-separated list of subdirs that MUST NOT appear "
                         "in the resulting work_path. Fails loudly if they do.")

    pl = sub.add_parser("list")
    pl.add_argument("--workspace", required=True)

    pr = sub.add_parser("remove")
    pr.add_argument("--workspace", required=True)
    pr.add_argument("--path", required=True)

    pcu = sub.add_parser("cleanup")
    pcu.add_argument("--workspace", required=True)
    pcu.add_argument("--age-days", type=int, default=7)

    return p


DISPATCH = {
    "create": cmd_create,
    "list": cmd_list,
    "remove": cmd_remove,
    "cleanup": cmd_cleanup,
}


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = DISPATCH[args.cmd](args)
    except SystemExit:
        raise
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
