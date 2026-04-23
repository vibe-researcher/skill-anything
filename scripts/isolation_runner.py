#!/usr/bin/env python3
"""Validate sub-agent isolation before/after execution.

Called by PreToolUse hook (--preflight) and PostToolUse hook (--verify) to
enforce that sub-agents run in physically-separate directories and do not
leak into parts of the workspace they were supposed to be blind to.

Subcommands:
    preflight     Before the Task tool runs, confirm the planned work_path
                  is a valid isolation dir (not workspace itself, contains
                  a manifest, exclude_guards hold).
    verify-post   After the Task tool returns, confirm the OSR's work_path
                  matches the expected manifest and files_read respects the
                  exclude_guards.
    term-leakage  Scan an eval-tasks.json for Skill-internal terminology.
                  Used by Eval Designer workflow to catch (P06) breach.

All subcommands print a single JSON object on stdout. Exit 0 on PASS, 1 on FAIL.

Usage:
    python scripts/isolation_runner.py preflight --work-path <path> \\
        --expected-purpose runner-with-iter5-t3 \\
        --forbid skills  # for without_skill runner

    python scripts/isolation_runner.py verify-post --work-path <path> \\
        --osr-file <osr.json> --forbid skills

    python scripts/isolation_runner.py term-leakage \\
        --eval-tasks <path> --terms-file <blocklist.txt>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def _issue(severity: str, message: str, **extra) -> dict:
    return {"severity": severity, "message": message, **extra}


def cmd_preflight(args) -> tuple[int, dict]:
    wp = Path(args.work_path).resolve()
    issues = []

    if not wp.exists():
        return 1, {"ok": False, "issues": [
            _issue("critical", f"work_path does not exist: {wp}")
        ]}

    if not wp.is_dir():
        return 1, {"ok": False, "issues": [
            _issue("critical", f"work_path is not a directory: {wp}")
        ]}

    manifest_path = wp / ".isolation.json"
    if not manifest_path.exists():
        return 1, {"ok": False, "issues": [
            _issue("critical", f".isolation.json missing: {manifest_path}",
                   suggestion="create the work_path via worktree_helper.py create")
        ]}

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return 1, {"ok": False, "issues": [
            _issue("critical", f".isolation.json is corrupt: {e}")
        ]}

    if args.expected_purpose and manifest.get("purpose") != args.expected_purpose:
        issues.append(_issue(
            "warn",
            f"purpose mismatch: manifest={manifest.get('purpose')!r} "
            f"expected={args.expected_purpose!r}",
        ))

    forbid = [x for x in (args.forbid or "").split(",") if x.strip()]
    for guard in forbid:
        if (wp / guard).exists():
            issues.append(_issue(
                "critical",
                f"forbidden directory {guard!r} present in work_path",
                suggestion="isolation breach — re-create work_path with exclude-guard",
            ))

    critical = [i for i in issues if i.get("severity") == "critical"]
    return (1 if critical else 0), {
        "ok": not critical,
        "work_path": str(wp),
        "manifest": manifest,
        "issues": issues,
    }


def cmd_verify_post(args) -> tuple[int, dict]:
    wp = Path(args.work_path).resolve()
    osr_path = Path(args.osr_file)
    forbid = [x for x in (args.forbid or "").split(",") if x.strip()]
    issues = []

    if not osr_path.exists():
        return 1, {"ok": False, "issues": [
            _issue("critical", f"OSR file not found: {osr_path}")
        ]}
    osr = json.loads(osr_path.read_text())

    # Check the work_path the agent reported matches what we expected
    reported_wp = osr.get("work_path") or osr.get("agent_env", {}).get("cwd")
    if reported_wp and Path(reported_wp).resolve() != wp:
        issues.append(_issue(
            "warn",
            f"OSR reports work_path={reported_wp!r} but expected {wp!r}",
        ))

    # Check files_read against the forbid list
    files_read = osr.get("files_read") or []
    leaks = []
    for fr in files_read:
        for guard in forbid:
            if f"/{guard}/" in fr or fr.startswith(f"{guard}/") or fr == guard:
                leaks.append({"file": fr, "guard": guard})
    if leaks:
        issues.append(_issue(
            "critical",
            f"forbidden reads detected: {len(leaks)} files under guarded dirs",
            leaks=leaks,
        ))

    # Isolation_env_path coherence for eval_designer
    if osr.get("agent_type") == "eval_designer":
        iso = osr.get("isolation_env_path", "")
        if "skills" in Path(iso).parts:
            issues.append(_issue(
                "critical",
                f"eval_designer isolation_env_path contains 'skills': {iso}",
            ))

    critical = [i for i in issues if i.get("severity") == "critical"]
    return (1 if critical else 0), {
        "ok": not critical,
        "work_path": str(wp),
        "issues": issues,
    }


def cmd_term_leakage(args) -> tuple[int, dict]:
    """Scan eval-tasks.json description fields for Skill-internal terms."""
    tasks_path = Path(args.eval_tasks)
    if not tasks_path.exists():
        return 1, {"ok": False, "issues": [
            _issue("critical", f"eval-tasks.json not found: {tasks_path}")
        ]}
    tasks = json.loads(tasks_path.read_text())

    if args.terms_file and Path(args.terms_file).exists():
        terms = [l.strip() for l in Path(args.terms_file).read_text().splitlines()
                 if l.strip() and not l.startswith("#")]
    else:
        terms = [t.strip() for t in (args.terms or "").split(",") if t.strip()]

    if not terms:
        return 0, {"ok": True, "note": "no term blocklist given — scan skipped"}

    leaks = []
    patterns = [(t, re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE)) for t in terms]
    for task in tasks if isinstance(tasks, list) else []:
        desc = task.get("description", "")
        eb = task.get("expectedBehavior", "")
        if isinstance(eb, list):
            eb = " ".join(eb)
        blob = f"{desc}\n{eb}"
        for term, pat in patterns:
            if pat.search(blob):
                leaks.append({"task_id": task.get("id"), "term": term})

    ok = not leaks
    return (0 if ok else 1), {
        "ok": ok,
        "tasks_scanned": len(tasks) if isinstance(tasks, list) else 0,
        "terms_checked": len(terms),
        "leaks": leaks,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="isolation_runner")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("preflight")
    pp.add_argument("--work-path", required=True)
    pp.add_argument("--expected-purpose", default="")
    pp.add_argument("--forbid", default="", help="Comma-separated forbidden subdirs")

    pv = sub.add_parser("verify-post")
    pv.add_argument("--work-path", required=True)
    pv.add_argument("--osr-file", required=True)
    pv.add_argument("--forbid", default="")

    pt = sub.add_parser("term-leakage")
    pt.add_argument("--eval-tasks", required=True)
    group = pt.add_mutually_exclusive_group()
    group.add_argument("--terms", default="",
                       help="Comma-separated inline list of forbidden terms")
    group.add_argument("--terms-file", default="",
                       help="Path to a file with one forbidden term per line")

    return p


DISPATCH = {
    "preflight": cmd_preflight,
    "verify-post": cmd_verify_post,
    "term-leakage": cmd_term_leakage,
}


def main() -> int:
    args = build_parser().parse_args()
    try:
        code, result = DISPATCH[args.cmd](args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
