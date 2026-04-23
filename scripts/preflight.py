#!/usr/bin/env python3
"""Preflight check run at Orchestrator startup and on session resume.

Checks the workspace is coherent and decides what the Orchestrator should do
next. The Orchestrator reads this script's stdout (a single JSON object) and
acts accordingly — it does NOT need to inspect the workspace itself.

Purpose:
    * Fail fast if prerequisites are missing (schemas, workspace dirs).
    * Detect whether this is first run vs. resume.
    * On resume, extract last event + next_action_hint so the Orchestrator
      can continue from a Markov state (no context memory required).
    * Emit a resume-event so the audit trail records the session transition.

Usage:
    python scripts/preflight.py <workspace> [--repo-url <url>]
                                [--context-mode minimal|rich]
                                [--no-emit-event]

Output (stdout, one JSON object):
    {
      "ok": true/false,
      "workspace": "...",
      "mode": "first_run" | "resume" | "fresh_init_needed",
      "state": {...},         // present if resume
      "last_event": {...},    // present if resume
      "next_action_hint": "...",
      "issues": [{severity, message, suggestion}],
      "recommended_action": "..."
    }

Exit codes:
    0 — all checks passed, Orchestrator may proceed
    1 — blocking issues found (see issues[] with severity=critical)
    2 — invocation / IO error
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"

REQUIRED_SCHEMAS = [
    "state.schema.json",
    "iteration.schema.json",
    "event.schema.json",
    "eval-task.schema.json",
    "osr-common.schema.json",
    "osr-researcher.schema.json",
    "osr-skill-writer.schema.json",
    "osr-eval-designer.schema.json",
    "osr-runner.schema.json",
    "osr-grader.schema.json",
]


def _issue(severity: str, message: str, suggestion: str = "") -> dict:
    d = {"severity": severity, "message": message}
    if suggestion:
        d["suggestion"] = suggestion
    return d


def _check_schemas(issues: list) -> None:
    if not SCHEMAS_DIR.exists():
        issues.append(_issue(
            "critical",
            f"schemas/ directory not found at {SCHEMAS_DIR}",
            "ensure you are running from the skill-anything repo root, or reinstall",
        ))
        return
    missing = []
    for name in REQUIRED_SCHEMAS:
        if not (SCHEMAS_DIR / name).exists():
            missing.append(name)
    if missing:
        issues.append(_issue(
            "critical",
            f"missing schema files: {missing}",
            "pull the latest schemas/ from the repo",
        ))


def _check_workspace_layout(ws: Path, issues: list) -> None:
    if not ws.exists():
        issues.append(_issue(
            "info",
            f"workspace {ws} does not exist — first_run",
            "run 'state_manager.py <ws> init --repo-url <url>' to initialize",
        ))
        return
    for sub in ["state", "state/iterations", "state/surprises", "state/rejections", "notes"]:
        p = ws / sub
        if not p.exists():
            issues.append(_issue(
                "warn",
                f"workspace missing subdir {sub}",
                "state_manager init re-creates these; run init with --force if safe",
            ))


def _check_git_status(ws: Path, issues: list) -> None:
    """If workspace or repo root is in a git repo, warn on dirty tree.

    This is advisory only — not blocking, because in-progress distillation
    legitimately produces uncommitted files.
    """
    for root in (ws, REPO_ROOT):
        if not root.exists():
            continue
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                lines = out.stdout.strip().splitlines()
                if len(lines) > 20:
                    issues.append(_issue(
                        "info",
                        f"{root.name}: {len(lines)} uncommitted changes — large delta",
                    ))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # no git, or hung


def _load_state(ws: Path) -> dict | None:
    p = ws / "state.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"state.json is corrupt: {e}") from e


def _load_last_event(ws: Path, event_id: str | None) -> dict | None:
    p = ws / "state" / "events.jsonl"
    if not p.exists():
        return None
    try:
        content = p.read_text().strip().splitlines()
    except OSError:
        return None
    if not content:
        return None
    last = json.loads(content[-1])
    if event_id and last.get("event_id") != event_id:
        # last_event_id in state.json drifted from actual file tail — pick actual tail
        # and surface as an anomaly.
        return {**last, "_drift_warning": True}
    return last


def _compute_next_action_hint(state: dict, last_event: dict | None) -> str:
    """Deterministic map from (phase, last_event_type) -> next action hint.

    Kept compact so the Orchestrator can follow without reading prose.
    """
    phase = state.get("phase", "init")
    evt = (last_event or {}).get("event_type", "")
    iter_n = state.get("current_iteration", 0)

    if phase == "init":
        if not state.get("research_done"):
            return "phase-transition to 'research'; spawn Researcher(s)"
        return "phase-transition to 'generate'"
    if phase == "research":
        if state.get("research_done"):
            return "phase-transition to 'generate'"
        return "spawn Researcher(s) for uncovered directions, OR mark research_done"
    if phase == "generate":
        if not state.get("generation_done"):
            return "spawn Skill Writer; then spawn Eval Designer (isolated); then phase-transition to 'iterate'"
        return "phase-transition to 'iterate' and start iter-1"
    if phase == "iterate":
        # Within an iteration, decide from last event
        if evt in ("", "phase_transition", "snapshot_created"):
            return f"begin iter-{iter_n + 1}: spawn Runners for all eval tasks (with_skill + without_skill)"
        if evt == "task_spawned":
            return "collect returning OSRs; validate each with osr_validate.py"
        if evt == "osr_returned":
            return "continue collecting OSRs; when Runner batch complete, run blind_eval.py then spawn Grader"
        if evt == "osr_rejected":
            return "re-spawn the rejected sub-agent with constraint note, OR investigate the cause"
        if evt == "guardrail_tripped":
            return "READ state.guardrail_flags and act; may require investigator spawn"
        return "inspect state/events.jsonl for pending work"
    if phase == "done":
        return "distillation complete; publish Skill via register_skill.py if desired"
    if phase == "aborted":
        return "manual review needed; see guardrail_flags for reason"
    return "unknown phase — investigate"


def _emit_resume_event(ws: Path, state: dict, new_session_id: str) -> None:
    ev = {
        "event_id": str(uuid.uuid4()),
        "ts": state.get("last_checkpoint_at", ""),
        "phase": state.get("phase", "init"),
        "event_type": "human_note",
        "summary": f"preflight resume: old_session={state.get('session_id','?')[:8]} -> new={new_session_id[:8]}",
        "iter": state.get("current_iteration", 0),
        "payload": {
            "old_session_id": state.get("session_id"),
            "new_session_id": new_session_id,
        },
    }
    events_path = ws / "state" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    state["session_id"] = new_session_id
    state["last_event_id"] = ev["event_id"]
    (ws / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2)
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="preflight")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("--repo-url", default="",
                    help="Required for first_run; ignored on resume")
    ap.add_argument("--context-mode", choices=["minimal", "rich"], default="minimal")
    ap.add_argument("--no-emit-event", action="store_true",
                    help="Do not emit a session-resume event (for dry-run)")
    args = ap.parse_args()

    ws: Path = args.workspace.resolve()
    issues: list = []

    _check_schemas(issues)
    _check_workspace_layout(ws, issues)
    _check_git_status(ws, issues)

    # Abort if any critical issue (missing schemas = cannot proceed)
    if any(i["severity"] == "critical" for i in issues):
        result = {
            "ok": False,
            "workspace": str(ws),
            "mode": "fresh_init_needed",
            "issues": issues,
            "recommended_action": "resolve critical issues listed above before running",
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1

    try:
        state = _load_state(ws)
    except RuntimeError as e:
        issues.append(_issue("critical", str(e),
                             "restore state.json from git OR run state_migrate.py"))
        result = {
            "ok": False,
            "workspace": str(ws),
            "mode": "fresh_init_needed",
            "issues": issues,
            "recommended_action": "restore or migrate state.json",
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1

    if state is None:
        result = {
            "ok": True,
            "workspace": str(ws),
            "mode": "first_run",
            "issues": issues,
            "recommended_action": (
                f"run: python scripts/state_manager.py {ws} init "
                f"--repo-url <url> --context-mode {args.context_mode}"
            ),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    # Resume path
    last_event = _load_last_event(ws, state.get("last_event_id"))
    if last_event and last_event.get("_drift_warning"):
        issues.append(_issue(
            "warn",
            "state.last_event_id drifted from events.jsonl tail",
            "an event was appended without updating state.json; trusting events.jsonl",
        ))

    next_hint = _compute_next_action_hint(state, last_event)

    new_session_id = str(uuid.uuid4())
    is_new_session = state.get("session_id") != new_session_id
    if is_new_session and not args.no_emit_event:
        try:
            _emit_resume_event(ws, state, new_session_id)
        except OSError as e:
            issues.append(_issue("warn", f"could not emit resume event: {e}"))

    result = {
        "ok": True,
        "workspace": str(ws),
        "mode": "resume",
        "state": {
            "session_id": state.get("session_id"),
            "phase": state.get("phase"),
            "context_mode": state.get("context_mode"),
            "current_iteration": state.get("current_iteration"),
            "research_done": state.get("research_done"),
            "generation_done": state.get("generation_done"),
            "scores_history": state.get("scores_history", [])[-5:],  # last 5 only
            "pending_surprises_count": len(
                state.get("open_channels", {}).get("pending_surprises", [])
            ),
            "guardrail_flags": state.get("guardrail_flags", []),
        },
        "last_event": last_event,
        "next_action_hint": next_hint,
        "issues": issues,
        "recommended_action": next_hint,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
