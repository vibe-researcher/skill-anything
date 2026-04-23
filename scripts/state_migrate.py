#!/usr/bin/env python3
"""Migrate a v1 workspace to v2 layout.

v1 artifacts:
    workspace/orchestrator-state.json
    workspace/results.tsv
    workspace/evals/results/iter-N/{eval-results,grader-scores,iteration-summary}.json

v2 produces (alongside — does not delete v1 files):
    workspace/state.json                        (main Markov state)
    workspace/state/events.jsonl                (synthesized from v1 history)
    workspace/state/iterations/iter-N.json      (one per historic iteration)
    workspace/state/{surprises,rejections}/     (empty dirs)
    workspace/notes/                            (empty dir)

The migration is **idempotent & non-destructive by default**:
    * If state.json already exists, refuses unless --force
    * Never deletes orchestrator-state.json or results.tsv

Usage:
    python scripts/state_migrate.py <workspace> [--force] [--repo-url <url>]

Prints a summary JSON. Exit 0 on success.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _read_v1_state(ws: Path) -> dict:
    p = ws / "orchestrator-state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _read_v1_tsv(ws: Path) -> list[dict]:
    p = ws / "results.tsv"
    if not p.exists():
        return []
    rows: list[dict] = []
    lines = p.read_text().strip().splitlines()
    if not lines:
        return []
    # Skip header
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            rows.append({
                "iter": int(parts[0]),
                "composite": float(parts[1]),
                "cost_usd": float(parts[2]),
                "status": parts[3] if len(parts) > 3 else "keep",
                "description": parts[4] if len(parts) > 4 else "",
            })
        except ValueError:
            continue
    return rows


def _find_iter_dirs(ws: Path) -> list[tuple[int, Path]]:
    d = ws / "evals" / "results"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.name.startswith("iter-") and p.is_dir():
            try:
                n = int(p.name.split("-", 1)[1])
                out.append((n, p))
            except (ValueError, IndexError):
                pass
    return out


def _synthesize_iteration_json(iter_n: int, iter_dir: Path) -> dict:
    """Reconstruct a minimal iter-N.json from v1 artifacts."""
    data: dict = {
        "iter": iter_n,
        "started_at": _now_iso(),
        "completed_at": _now_iso(),
    }
    # Load per-task composite scores if we have them
    summary_file = iter_dir / "iteration-summary.json"
    if summary_file.exists():
        try:
            summary = json.loads(summary_file.read_text())
            data["composite_score"] = summary.get("composite_score")
            data["regressions"] = summary.get("regressions", [])
        except json.JSONDecodeError:
            pass
    # Extract tool counts from eval-results.json if available (as proxy for runs)
    er = iter_dir / "eval-results.json"
    if er.exists():
        try:
            er_data = json.loads(er.read_text())
            runs = []
            for t in er_data.get("tasks", []):
                runs.append({
                    "task_id": t.get("taskId", ""),
                    "with_skill": {
                        "runner_return_path": "<v1: not available>",
                        "subagent_log_path": "<v1: not available>",
                        "status": "success",
                        "output_files": [],
                    },
                    "without_skill": {
                        "runner_return_path": "<v1: not available>",
                        "subagent_log_path": "<v1: not available>",
                        "status": "success",
                        "output_files": [],
                    },
                    "tool_counts": {
                        "with": t.get("withSkill", {}).get("toolUseCount", 0),
                        "without": t.get("withoutSkill", {}).get("toolUseCount", 0),
                        "source": "self_report",
                    },
                })
            if runs:
                data["runs"] = runs
        except json.JSONDecodeError:
            pass
    # Grader digest path if present
    dig = iter_dir / "osr-grader-digest.json"
    if dig.exists():
        data["grader"] = {"osr_digest_path": str(dig)}
    else:
        sf = iter_dir / "grader-scores.json"
        if sf.exists():
            data["grader"] = {
                "osr_digest_path": "<v1: no digest, see scores_file>",
                "scores_file": str(sf),
            }
    return data


def _synthesize_events(ws: Path, v1_state: dict, tsv_rows: list[dict],
                       iter_dirs: list[tuple[int, Path]]) -> list[dict]:
    """Build a plausible events.jsonl from v1 history."""
    events: list[dict] = []

    # Initial init
    events.append({
        "event_id": str(uuid.uuid4()),
        "ts": _now_iso(),
        "phase": "init",
        "event_type": "phase_transition",
        "summary": "v2 migration: init reconstructed",
        "iter": 0,
        "payload": {"from": "none", "to": "init"},
    })

    if v1_state.get("research_done"):
        events.append({
            "event_id": str(uuid.uuid4()),
            "ts": _now_iso(),
            "phase": "research",
            "event_type": "phase_transition",
            "summary": "v2 migration: research completed",
            "iter": 0,
        })

    if v1_state.get("generation_done"):
        events.append({
            "event_id": str(uuid.uuid4()),
            "ts": _now_iso(),
            "phase": "generate",
            "event_type": "phase_transition",
            "summary": "v2 migration: generation completed",
            "iter": 0,
        })

    # One snapshot event per historic iteration score
    for row in tsv_rows:
        events.append({
            "event_id": str(uuid.uuid4()),
            "ts": _now_iso(),
            "phase": "iterate",
            "event_type": "snapshot_created",
            "summary": f"v2 migration: iter-{row['iter']} composite={row['composite']:.4f}",
            "iter": row["iter"],
            "payload": {
                "composite": row["composite"],
                "cost_usd": row["cost_usd"],
                "status_v1": row["status"],
            },
        })

    return events


def migrate(ws: Path, force: bool, repo_url: str) -> dict:
    state_file = ws / "state.json"
    if state_file.exists() and not force:
        raise SystemExit(
            f"state.json already exists at {state_file}. Use --force to overwrite."
        )

    # Create dir skeleton
    for sub in ("state", "state/iterations", "state/surprises",
                "state/rejections", "notes"):
        (ws / sub).mkdir(parents=True, exist_ok=True)

    v1 = _read_v1_state(ws)
    tsv = _read_v1_tsv(ws)
    iter_dirs = _find_iter_dirs(ws)

    # Resolve repo_url
    final_repo_url = repo_url or v1.get("repo_url") or "unknown"

    # Resolve phase
    phase = v1.get("phase", "init")
    if phase not in ("init", "research", "generate", "iterate", "done", "aborted"):
        phase = "iterate" if tsv else "init"

    # Build scores_history from tsv (authoritative) with fallback to v1_state.iteration.scores
    scores_history: list[dict] = []
    for row in tsv:
        entry = {"iter": row["iter"], "composite": row["composite"]}
        if row["cost_usd"] is not None:
            entry["cost_usd"] = row["cost_usd"]
        scores_history.append(entry)
    if not scores_history:
        v1_scores = (v1.get("iteration") or {}).get("scores") or []
        for i, s in enumerate(v1_scores, start=1):
            scores_history.append({"iter": i, "composite": s})

    current_iter = max([s["iter"] for s in scores_history] + [0])
    if v1.get("iteration", {}).get("current"):
        current_iter = max(current_iter, v1["iteration"]["current"])

    state = {
        "schema_version": "2.0.0",
        "session_id": str(uuid.uuid4()),
        "repo_url": final_repo_url,
        "phase": phase,
        "context_mode": "minimal",
        "current_iteration": current_iter,
        "research_done": bool(v1.get("research_done", False)),
        "generation_done": bool(v1.get("generation_done", False)),
        "scores_history": scores_history,
        "open_channels": {
            "pending_surprises": [],
            "anomalies_count_by_type": {},
            "meta_observations_digest": [
                f"migrated from v1; v1 notes: {(v1.get('notes') or '')[:150]}"
            ] if v1.get("notes") else [],
        },
        "guardrail_flags": [],
        "last_checkpoint_at": _now_iso(),
        "next_action_hint": "run preflight.py to recompute next action post-migration",
    }

    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    # events.jsonl
    events = _synthesize_events(ws, v1, tsv, iter_dirs)
    events_path = ws / "state" / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    state["last_event_id"] = events[-1]["event_id"] if events else ""
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    # per-iteration files
    iter_files_written = []
    for n, d in iter_dirs:
        iter_data = _synthesize_iteration_json(n, d)
        p = ws / "state" / "iterations" / f"iter-{n}.json"
        p.write_text(json.dumps(iter_data, indent=2, ensure_ascii=False))
        iter_files_written.append(str(p))

    return {
        "ok": True,
        "state_path": str(state_file),
        "events_count": len(events),
        "iteration_files_written": len(iter_files_written),
        "current_iteration": current_iter,
        "scores_migrated": len(scores_history),
        "phase": phase,
        "v1_files_preserved": [
            str(ws / "orchestrator-state.json"),
            str(ws / "results.tsv"),
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="state_migrate")
    ap.add_argument("workspace", type=Path)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--repo-url", default="")
    args = ap.parse_args()

    ws = args.workspace.resolve()
    if not ws.exists():
        print(json.dumps({"ok": False, "error": f"workspace not found: {ws}"}))
        return 2
    try:
        result = migrate(ws, args.force, args.repo_url)
    except SystemExit as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
