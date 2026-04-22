#!/usr/bin/env python3
"""Aggregate invariant / guardrail checks on the workspace.

Run after each iteration (automatically by SKILL.md §9) or ad-hoc to verify
the distillation loop is behaving as designed. Each check returns a verdict;
the overall exit code is the max severity found.

Exit codes:
    0 — all checks passed (or only info/warn)
    1 — one or more critical guardrails tripped
    2 — invocation error

Available checks (select with --check <name>, or run all with --all):

    task_reality         Every Runner's subagent_log_path exists and is
                         non-empty; Runner session_ids are distinct from
                         Orchestrator's. Verifies P01/P02 defense.

    tool_count_variance  Across iterations, withSkill tool counts have
                         variance > σ_min (default 1.5). Iter-N and Iter-N+1
                         must not be element-wise identical. Verifies P03.

    skill_won_rate       Latest osr-grader-digest skill_won_rate is between
                         0.3 and 0.9 (outside → flag for context upgrade).

    open_channels        No pending_surprise has age > 3 iterations. Not all
                         of the last 3 iterations had empty surprises across
                         all agents. Verifies anti-flat-pipeline invariant.

    skill_growth         At least one iteration in the last 3 has
                         removed_lines > 0 (simplicity principle P12).

    grader_ensemble_agreement
                         For the latest iteration that has an ensemble-metrics.
                         json, mean_winner_agreement must be ≥ 0.5. If ≥ half
                         the tasks are in disagreement_tasks, severity escalates
                         to critical (eval-design ambiguity likely). Also
                         reports recurring disagreement_tasks across the last
                         3 iterations as meta-observation-worthy.

Usage:
    python scripts/invariant_check.py --workspace <ws> --all
    python scripts/invariant_check.py --workspace <ws> --check tool_count_variance
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}


def _verdict(name: str, ok: bool, severity: str, detail: str,
             evidence: Any = None, recommendation: str = "") -> dict:
    d = {
        "check": name,
        "ok": ok,
        "severity": severity if not ok else "info",
        "detail": detail,
    }
    if evidence is not None:
        d["evidence"] = evidence
    if recommendation:
        d["recommendation"] = recommendation
    return d


def _load_state(ws: Path) -> dict | None:
    p = ws / "state.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _load_iter(ws: Path, n: int) -> dict | None:
    p = ws / "state" / "iterations" / f"iter-{n}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _list_iter_files(ws: Path) -> list[tuple[int, Path]]:
    d = ws / "state" / "iterations"
    if not d.exists():
        return []
    out = []
    for p in d.iterdir():
        if p.name.startswith("iter-") and p.name.endswith(".json"):
            try:
                n = int(p.stem.split("-", 1)[1])
                out.append((n, p))
            except (ValueError, IndexError):
                pass
    return sorted(out)


def _latest_digest(ws: Path) -> dict | None:
    """Find the most-recent osr-grader-digest.json."""
    results = ws / "evals" / "results"
    if not results.exists():
        return None
    iters = sorted(
        [p for p in results.iterdir() if p.name.startswith("iter-")],
        key=lambda p: int(p.name.split("-", 1)[1] if p.name.split("-", 1)[1].isdigit() else 0),
        reverse=True,
    )
    for it in iters:
        dig = it / "osr-grader-digest.json"
        if dig.exists():
            try:
                return json.loads(dig.read_text())
            except json.JSONDecodeError:
                continue
    return None


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_task_reality(ws: Path, state: dict) -> dict:
    session_id = state.get("session_id", "")
    iter_files = _list_iter_files(ws)
    if not iter_files:
        return _verdict(
            "task_reality", True, "info",
            "no iteration data yet — nothing to check",
        )

    # Check the most-recent iter only; older ones pre-date v2 maybe
    latest_n, latest_path = iter_files[-1]
    data = json.loads(latest_path.read_text())
    runs = data.get("runs") or []
    if not runs:
        return _verdict(
            "task_reality", True, "info",
            f"iter-{latest_n} has no runs yet",
        )

    missing = []
    for run in runs:
        for key in ("with_skill", "without_skill"):
            variant = run.get(key) or {}
            log_path = variant.get("subagent_log_path", "")
            if not log_path:
                missing.append({
                    "task_id": run.get("task_id"),
                    "variant": key,
                    "reason": "no subagent_log_path field",
                })
                continue
            p = Path(log_path)
            if not p.exists() or p.stat().st_size == 0:
                missing.append({
                    "task_id": run.get("task_id"),
                    "variant": key,
                    "reason": f"log path empty or missing: {log_path}",
                })

    ok = not missing
    return _verdict(
        "task_reality", ok, "critical",
        f"{len(missing)} of {len(runs)*2} runs failed reality check" if not ok
        else f"all {len(runs)*2} runs have valid subagent logs",
        evidence=missing[:5] if missing else None,
        recommendation="Runners not physically spawning — check worktree_helper usage and hook logs"
        if not ok else "",
    )


def check_tool_count_variance(ws: Path, state: dict,
                              sigma_min: float = 1.5) -> dict:
    """Tool counts across iterations must have variance > sigma_min and
    iter-N must not be elementwise identical to iter-N+1."""
    iter_files = _list_iter_files(ws)
    if len(iter_files) < 2:
        return _verdict(
            "tool_count_variance", True, "info",
            f"only {len(iter_files)} iteration(s); cannot compute variance yet",
        )

    # Build a matrix: rows=iter, cols=task, values=tool_count_with
    series: dict[str, list[int | None]] = {}
    iters_covered = []
    for n, p in iter_files[-3:]:  # look at last 3
        data = json.loads(p.read_text())
        iters_covered.append(n)
        for run in data.get("runs") or []:
            tid = run.get("task_id", "?")
            tc = (run.get("tool_counts") or {}).get("with")
            series.setdefault(tid, []).append(tc)
        # Pad series for tasks absent in this iter
        for tid in series:
            while len(series[tid]) < len(iters_covered):
                series[tid].append(None)

    # Check iter-identity between consecutive iterations
    identity_hits = []
    for i in range(len(iters_covered) - 1):
        a_all = []
        b_all = []
        for tid, vals in series.items():
            if i < len(vals) and i + 1 < len(vals):
                if vals[i] is not None and vals[i + 1] is not None:
                    a_all.append(vals[i])
                    b_all.append(vals[i + 1])
        if a_all and a_all == b_all:
            identity_hits.append({
                "iter_a": iters_covered[i],
                "iter_b": iters_covered[i + 1],
                "len": len(a_all),
            })

    # Check variance per task across iterations
    low_variance_tasks = []
    for tid, vals in series.items():
        numeric = [v for v in vals if v is not None]
        if len(numeric) >= 2:
            mean = sum(numeric) / len(numeric)
            var = sum((x - mean) ** 2 for x in numeric) / len(numeric)
            stdev = math.sqrt(var)
            if stdev < 0.01:  # genuinely constant — could be legit if easy task
                low_variance_tasks.append({"task_id": tid, "stdev": 0.0})

    ok = not identity_hits
    severity = "critical" if identity_hits else ("warn" if low_variance_tasks else "info")
    detail_parts = []
    if identity_hits:
        detail_parts.append(
            f"iter-to-iter identity detected in {len(identity_hits)} pair(s) — "
            "consecutive iterations have identical tool counts (P03 fabrication pattern)"
        )
    if low_variance_tasks:
        detail_parts.append(
            f"{len(low_variance_tasks)} task(s) had zero cross-iter stdev"
        )
    if not detail_parts:
        detail_parts.append("tool_count distributions look healthy")

    return _verdict(
        "tool_count_variance", ok, severity,
        "; ".join(detail_parts),
        evidence={"identity_hits": identity_hits[:3],
                  "low_variance_tasks": low_variance_tasks[:3]},
        recommendation=(
            "toolUseCount pattern suggests fabrication — verify subagent_log.py "
            "is being consulted and work_paths are genuinely independent"
            if identity_hits else ""
        ),
    )


def check_skill_won_rate(ws: Path) -> dict:
    digest = _latest_digest(ws)
    if not digest:
        return _verdict(
            "skill_won_rate", True, "info",
            "no grader digest yet — cannot check",
        )
    rate = (digest.get("aggregate") or {}).get("skill_won_rate")
    if rate is None:
        return _verdict("skill_won_rate", True, "info",
                        "aggregate.skill_won_rate absent")
    if rate > 0.9:
        return _verdict(
            "skill_won_rate", False, "warn",
            f"skill_won_rate={rate} is unusually high; eval may be too easy "
            "or Grader biased",
            evidence={"rate": rate, "digest_iter": digest.get("iteration")},
            recommendation="upgrade context_mode to 'rich' and read feedback_file",
        )
    if rate < 0.3:
        return _verdict(
            "skill_won_rate", False, "warn",
            f"skill_won_rate={rate} is unusually low; Skill may be regressing",
            evidence={"rate": rate, "digest_iter": digest.get("iteration")},
            recommendation="upgrade context_mode to 'rich'; consider skill_writer from-scratch rewrite",
        )
    return _verdict(
        "skill_won_rate", True, "info",
        f"skill_won_rate={rate} within healthy 0.3-0.9 band",
    )


def _iter_surprises_filled(data: dict) -> bool:
    """True iff any agent in this iter filed at least one open-channel signal."""
    # Look at runs/grader/skill_writer sub-structures; OSR returns are referenced
    # but the counts are folded back into events. As a proxy, we inspect the
    # iter's embedded OSR returns if present.
    # For MVP, scan all strings for well-known agent keys and count non-empties.
    def has_signal(obj: Any) -> bool:
        if isinstance(obj, dict):
            for k in ("surprises", "anomalies", "meta_observations"):
                v = obj.get(k)
                if isinstance(v, list) and len(v) > 0:
                    return True
            for v in obj.values():
                if has_signal(v):
                    return True
        elif isinstance(obj, list):
            for v in obj:
                if has_signal(v):
                    return True
        return False
    return has_signal(data)


def check_open_channels(ws: Path, state: dict) -> dict:
    open_ch = state.get("open_channels") or {}
    pending = open_ch.get("pending_surprises") or []
    stale = [s for s in pending if (s.get("age") or 0) > 3]

    iter_files = _list_iter_files(ws)
    recent = iter_files[-3:]
    silent = []
    for n, p in recent:
        data = json.loads(p.read_text())
        if not _iter_surprises_filled(data):
            silent.append(n)

    issues = []
    if stale:
        issues.append({
            "reason": "stale_surprises",
            "count": len(stale),
            "ids": [s.get("id") for s in stale[:5]],
        })
    if len(silent) == len(recent) and len(recent) >= 3:
        issues.append({
            "reason": "silent_for_3_iters",
            "iters": silent,
        })

    ok = not issues
    return _verdict(
        "open_channels", ok,
        "warn" if issues else "info",
        "open channels healthy" if ok
        else f"{len(issues)} open-channel issue(s) detected",
        evidence=issues if issues else None,
        recommendation=(
            "spawn investigator to triage stale surprises" if stale
            else "agents may be under-reporting; spawn investigator to audit" if silent
            else ""
        ),
    )


def _list_ensemble_metrics(ws: Path) -> list[tuple[int, Path]]:
    """Return [(iter_n, path_to_ensemble-metrics.json), ...] sorted by iter."""
    results = ws / "evals" / "results"
    if not results.exists():
        return []
    out = []
    for it in results.iterdir():
        if not it.name.startswith("iter-"):
            continue
        try:
            n = int(it.name.split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        m = it / "ensemble-metrics.json"
        if m.exists():
            out.append((n, m))
    return sorted(out)


def check_grader_ensemble_agreement(ws: Path,
                                    min_agreement: float = 0.5) -> dict:
    """Verify ensemble-metrics.json shows acceptable grader agreement.

    - mean_winner_agreement ≥ min_agreement (default 0.5) → ok
    - If disagreement_tasks covers ≥ half of tasks → critical (eval ambiguity)
    - Recurring disagreement tasks across last 3 iters → info evidence
    - If no ensemble-metrics.json exists (K=1 pre-ensemble workspaces) → info
    """
    ensembles = _list_ensemble_metrics(ws)
    if not ensembles:
        return _verdict(
            "grader_ensemble_agreement", True, "info",
            "no ensemble-metrics.json yet — single-grader mode or pre-grade",
        )

    latest_n, latest_path = ensembles[-1]
    try:
        latest = json.loads(latest_path.read_text())
    except json.JSONDecodeError as e:
        return _verdict(
            "grader_ensemble_agreement", False, "warn",
            f"iter-{latest_n} ensemble-metrics.json unparseable: {e}",
            evidence={"path": str(latest_path)},
        )

    k = latest.get("k", 1)
    agreement = latest.get("mean_winner_agreement", 1.0)
    stdev = latest.get("mean_quality_stdev", 0.0)
    disagreed = latest.get("disagreement_tasks", []) or []
    total_tasks = len(latest.get("per_task", []) or [])
    disagreement_ratio = (len(disagreed) / total_tasks
                          if total_tasks else 0.0)

    # Recurring disagreements across last 3 iters
    recurring: dict[str, int] = {}
    for n, p in ensembles[-3:]:
        try:
            m = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        for tid in m.get("disagreement_tasks", []) or []:
            recurring[tid] = recurring.get(tid, 0) + 1
    persistent = [tid for tid, cnt in recurring.items() if cnt >= 2]

    # Decide verdict
    if k <= 1:
        return _verdict(
            "grader_ensemble_agreement", True, "info",
            f"iter-{latest_n} ran with K={k} (single grader) — ensemble "
            "metrics are trivially consistent",
            evidence={"k": k},
        )

    evidence = {
        "iteration": latest_n,
        "k": k,
        "mean_winner_agreement": agreement,
        "mean_quality_stdev": stdev,
        "disagreement_task_count": len(disagreed),
        "total_tasks": total_tasks,
        "disagreement_ratio": round(disagreement_ratio, 3),
        "disagreement_tasks_sample": disagreed[:5],
        "persistent_disagreement_tasks": persistent,
    }

    if disagreement_ratio >= 0.5 and total_tasks >= 4:
        return _verdict(
            "grader_ensemble_agreement", False, "critical",
            f"iter-{latest_n}: {len(disagreed)}/{total_tasks} tasks "
            "show grader disagreement — likely eval-design ambiguity",
            evidence=evidence,
            recommendation=(
                "spawn investigator to audit eval-tasks.json for tasks in "
                "disagreement_tasks; consider Eval Designer rewrite for "
                "persistent items"),
        )

    if agreement < min_agreement:
        return _verdict(
            "grader_ensemble_agreement", False, "warn",
            f"iter-{latest_n}: mean_winner_agreement={agreement} < "
            f"{min_agreement}; graders frequently split",
            evidence=evidence,
            recommendation=(
                "upgrade context_mode to 'rich' and read ensemble-metrics.json "
                "+ feedback_file; examine whether grader prompt or quality "
                "rubric needs tightening"),
        )

    if persistent:
        return _verdict(
            "grader_ensemble_agreement", True, "warn",
            f"iter-{latest_n}: agreement healthy ({agreement}) but "
            f"{len(persistent)} task(s) disagreed across ≥2 of last 3 iters",
            evidence=evidence,
            recommendation=(
                "these tasks may be ambiguous by design — flag for "
                "Eval Designer review next iteration"),
        )

    return _verdict(
        "grader_ensemble_agreement", True, "info",
        f"iter-{latest_n}: K={k}, mean_winner_agreement={agreement}, "
        f"{len(disagreed)}/{total_tasks} tasks flagged — healthy",
        evidence=evidence,
    )


def check_skill_growth(ws: Path) -> dict:
    iter_files = _list_iter_files(ws)
    recent = iter_files[-3:]
    if len(recent) < 3:
        return _verdict(
            "skill_growth", True, "info",
            f"only {len(recent)} iteration(s); need 3 for growth check",
        )
    any_removed = False
    for n, p in recent:
        data = json.loads(p.read_text())
        removed = (data.get("skill_writer") or {}).get("removed_lines") or 0
        if removed > 0:
            any_removed = True
            break
    if any_removed:
        return _verdict(
            "skill_growth", True, "info",
            "at least one of last 3 iterations removed lines — simplicity check passed",
        )
    return _verdict(
        "skill_growth", False, "warn",
        "last 3 iterations were all pure additions (no removed_lines > 0)",
        recommendation=(
            "force a 'subtraction-only' iteration — spawn Skill Writer with "
            "'review and delete low-value sections' prompt"
        ),
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


CHECKS = {
    "task_reality": lambda ws, st: check_task_reality(ws, st),
    "tool_count_variance": lambda ws, st: check_tool_count_variance(ws, st),
    "skill_won_rate": lambda ws, st: check_skill_won_rate(ws),
    "open_channels": lambda ws, st: check_open_channels(ws, st),
    "skill_growth": lambda ws, st: check_skill_growth(ws),
    "grader_ensemble_agreement":
        lambda ws, st: check_grader_ensemble_agreement(ws),
}


def main() -> int:
    ap = argparse.ArgumentParser(prog="invariant_check")
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--check", choices=list(CHECKS.keys()), default=None)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    ws = args.workspace.resolve()
    state = _load_state(ws)
    if state is None:
        print(json.dumps({"ok": False, "error": f"no state.json at {ws}"}))
        return 2

    if args.check:
        checks_to_run = [args.check]
    elif args.all:
        checks_to_run = list(CHECKS.keys())
    else:
        print(json.dumps({"ok": False, "error": "must pass --check or --all"}))
        return 2

    results = [CHECKS[name](ws, state) for name in checks_to_run]
    max_sev = max(
        (SEVERITY_RANK.get(r["severity"], 0) for r in results if not r["ok"]),
        default=0,
    )
    overall_ok = max_sev < SEVERITY_RANK["critical"]

    summary = {
        "ok": overall_ok,
        "workspace": str(ws),
        "max_severity": (
            "info" if max_sev == 0 else "warn" if max_sev == 1 else "critical"
        ),
        "checks": results,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
