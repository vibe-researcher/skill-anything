#!/usr/bin/env python3
"""Verify Skill Writer changes are grounded in research knowledge, not patched
to specific eval tasks (P04 root-cause defense).

For each `changes_applied` entry in a Skill Writer OSR:

  1. knowledge_source_refs must cite real file paths and line ranges that
     exist in workspace/knowledge/.
  2. The cited text must have non-trivial lexical overlap with the change's
     rationale (token-Jaccard >= --min-overlap, default 0.15). Low overlap
     suggests the Writer cited unrelated knowledge just to satisfy the
     contract.
  3. rationale_short and target_section must NOT contain literal eval task
     ids — that would be a "write the fix for this exact test case" pattern.

Outputs a structured verdict to stdout. Exit 0 iff all changes pass.

Usage:
    python scripts/overfit_check.py --workspace <ws> \\
        --changes-file <path-to-osr-or-changes-json> \\
        [--eval-tasks <path>] [--min-overlap 0.15]

The --changes-file may be either:
  - A full osr-skill-writer.json (we extract .changes_applied)
  - A raw JSON array of changes
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|\w+")


def _tokens(text: str) -> set[str]:
    text = text.lower()
    return set(TOKEN_RE.findall(text))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _read_lines(path: Path, line_from: int, line_to: int) -> str:
    """Read inclusive line range from path (1-indexed)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    lo = max(1, line_from) - 1
    hi = min(len(lines), line_to)
    return "".join(lines[lo:hi])


def _load_changes(path: Path) -> list[dict]:
    """Load .changes_applied either from a full OSR file or a raw array."""
    obj = json.loads(path.read_text())
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        return obj.get("changes_applied", [])
    return []


def _load_task_ids(eval_tasks: Path | None) -> list[str]:
    if not eval_tasks or not eval_tasks.exists():
        return []
    try:
        tasks = json.loads(eval_tasks.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(tasks, list):
        return [t.get("id", "") for t in tasks if isinstance(t, dict) and t.get("id")]
    return []


def check_change(change: dict, ws: Path, task_ids: list[str],
                 min_overlap: float) -> dict:
    """Return a result dict for a single change."""
    change_id = change.get("change_id") or "?"
    ctype = change.get("type", "")
    rationale = change.get("rationale_short") or ""
    target_section = change.get("target_section") or ""
    refs = change.get("knowledge_source_refs") or []

    failures: list[dict] = []
    warnings: list[dict] = []

    # Skip deep checks for delete-type changes (refs may be empty)
    if ctype == "delete":
        if task_ids:
            for tid in task_ids:
                if re.search(rf"\b{re.escape(tid)}\b", rationale, re.IGNORECASE):
                    failures.append({
                        "reason": "task_id_literal_in_rationale",
                        "task_id": tid,
                    })
        return {
            "change_id": change_id,
            "type": ctype,
            "passed": not failures,
            "failures": failures,
            "warnings": warnings,
        }

    # 1. Refs exist and are non-empty for add/modify
    if not refs:
        failures.append({
            "reason": "empty_knowledge_source_refs",
            "suggestion": "add at least one (path, line_from, line_to) from knowledge/*.md",
        })
        return {
            "change_id": change_id,
            "type": ctype,
            "passed": False,
            "failures": failures,
            "warnings": warnings,
        }

    # 2. Each ref points to a real file and a valid line range
    ref_tokens: set[str] = set()
    for i, ref in enumerate(refs):
        p = ws / ref.get("path", "")
        lf, lt = ref.get("line_from", 0), ref.get("line_to", 0)
        if not p.exists():
            failures.append({
                "reason": "knowledge_file_missing",
                "ref_index": i,
                "path": str(p),
            })
            continue
        excerpt = _read_lines(p, lf, lt)
        if not excerpt.strip():
            failures.append({
                "reason": "empty_line_range",
                "ref_index": i,
                "path": str(p),
                "line_from": lf,
                "line_to": lt,
            })
            continue
        ref_tokens |= _tokens(excerpt)

    # 3. Lexical overlap between refs and the change's text
    change_tokens = _tokens(rationale + " " + target_section)
    overlap = _jaccard(ref_tokens, change_tokens)
    if ref_tokens and overlap < min_overlap:
        warnings.append({
            "reason": "low_knowledge_overlap",
            "overlap": round(overlap, 3),
            "min_required": min_overlap,
            "note": "cited knowledge section has weak lexical overlap with change "
                    "rationale; may be a pro-forma citation",
        })

    # 4. Task id literals
    blob = f"{rationale}\n{target_section}"
    for tid in task_ids:
        if re.search(rf"\b{re.escape(tid)}\b", blob, re.IGNORECASE):
            failures.append({
                "reason": "task_id_literal_in_rationale",
                "task_id": tid,
                "suggestion": (
                    "rewrite rationale as generalizable domain guidance, "
                    "not a patch for a specific eval task"
                ),
            })

    return {
        "change_id": change_id,
        "type": ctype,
        "passed": not failures,
        "overlap": round(overlap, 3) if ref_tokens else None,
        "failures": failures,
        "warnings": warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="overfit_check")
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--changes-file", type=Path, required=True)
    ap.add_argument("--eval-tasks", type=Path, default=None,
                    help="Defaults to <workspace>/evals/eval-tasks.json")
    ap.add_argument("--min-overlap", type=float, default=0.15)
    args = ap.parse_args()

    ws = args.workspace.resolve()
    if not ws.exists():
        print(json.dumps({"ok": False, "error": f"workspace not found: {ws}"}))
        return 2

    if not args.changes_file.exists():
        print(json.dumps({"ok": False, "error": f"changes file not found: {args.changes_file}"}))
        return 2

    eval_tasks = args.eval_tasks or (ws / "evals" / "eval-tasks.json")
    task_ids = _load_task_ids(eval_tasks)

    try:
        changes = _load_changes(args.changes_file)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad JSON: {e}"}))
        return 2

    results = [check_change(c, ws, task_ids, args.min_overlap) for c in changes]
    passed = sum(1 for r in results if r["passed"])
    all_ok = passed == len(results)

    summary = {
        "ok": all_ok,
        "total_changes": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "task_ids_scanned": len(task_ids),
        "min_overlap_required": args.min_overlap,
        "results": results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
