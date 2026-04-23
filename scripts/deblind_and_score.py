#!/usr/bin/env python3
"""De-blind grader scores, compute composite, check convergence.

Reads blind-grader-scores.json + blind-mapping.json + eval-results.json.
Writes:
    * grader-scores.json (full long-form detail, back-compat)
    * iteration-summary.json (compact human-readable summary)
    * osr-grader-digest.json (v2: schema-validated subset for Orchestrator)

Also (v2) appends a 'snapshot_created' event to state/events.jsonl so the
Orchestrator knows scoring completed and can read the digest without
searching.

Calls convergence.py internally.

Usage:
    python scripts/deblind_and_score.py <workspace> <iteration> [--cost <usd>]

stdout prints a compact summary for the Orchestrator.

---
Schema support (v2, post-lessons-learned 2026-04-23):

This scorer accepts two grader OSR shapes natively and dies loudly on
anything else, instead of silent composite=0:

1. **camelCase (legacy)** — a top-level JSON array OR dict with `tasks`/`scores`.
   Each entry: {taskId, winner, outputA.quality, outputB.quality, ...}.

2. **snake_case (current agents/grader.md contract)** — dict with `per_task`.
   Each entry: {task_id, winner, quality_a, quality_b, ...}.

The scorer normalises to a common internal shape before computing composite.
Empty input (no scores parsed) exits non-zero with an informative error.

---
Trajectory formula (v2):

`compute_trajectory` returns either a float in [0, 1] with **parity = 0.5**
(no clamp-collapse at 1:1 tool counts) or `None` if the signal is
unavailable (null tool_counts, zero without, super-runner aggregate).
`compute_composite` reweights to quality-only (weight 1.0) when trajectory
is None, so unavailable != 0.

---
Super-Runner detection:

If every task in an iteration shares the same (with, without) pair AND
there is more than one task, tool counts are session-aggregates, not
per-task. Trajectory is forced to None for all tasks and a warning is
emitted in iteration-summary.json.trajectory_degraded.

---
Regression policy (v2, pairs with SKILL.md §9 rewrite):

Computes two delta series per task:
- composite_delta = composite_N - composite_{N-1}
- quality_delta   = quality_N   - quality_{N-1}

Emits both in iteration-summary under `composite_regressions` and
`quality_regressions`. Only `quality_regressions` is safety-critical —
the Orchestrator's auto-rollback should fire on quality regression > 0.05,
not on composite alone. Composite-only regression + quality-positive =
`trajectory_regression_observed` event (warn, not block).
"""

import json
import math
import subprocess
import sys
from pathlib import Path


# ---------- Trajectory & composite ------------------------------------------

QUALITY_WEIGHT = 0.6
TRAJECTORY_WEIGHT = 0.4


def compute_trajectory(tool_calls_with, tool_calls_without):
    """Return a trajectory score in [0, 1] with parity at 0.5, or None.

    None is returned when the signal is genuinely unavailable (null input,
    zero baseline). None is NOT the same as 0 — callers must treat it
    distinctly (reweight composite to quality-only).

    Formula: symmetric log-ratio squashed to [-0.5, 0.5] then shifted.
    - 2x fewer tool calls → +0.25 (score 0.75)
    - 2x more  tool calls → -0.25 (score 0.25)
    - parity (1:1)        →  0.00 (score 0.50)

    The 1/4 divisor sets the saturation span: beyond ~16x either way the
    score clamps to 0 or 1. Within the typical 0.5x-2x working range the
    gradient is linear-enough that +/- 1 tool call at n≈10 moves the
    score by ~0.04 (vs 0.1 in the old formula at the same point).
    """
    if tool_calls_with is None or tool_calls_without is None:
        return None
    if tool_calls_without <= 0:
        return None
    ratio = tool_calls_with / max(1, tool_calls_without)
    if ratio <= 0:
        return None
    raw = -math.log2(ratio) / 4
    return max(0.0, min(1.0, 0.5 + raw))


def compute_composite(quality, tool_calls_with, tool_calls_without):
    """Return (composite, trajectory_available).

    composite is in [0, 1]. When trajectory is available, it is
    `0.6*(quality/5) + 0.4*trajectory`. When unavailable, composite is
    `quality/5` (quality-only; no silent-zero for trajectory).
    """
    q_norm = quality / 5.0
    traj = compute_trajectory(tool_calls_with, tool_calls_without)
    if traj is None:
        return round(q_norm, 4), False
    return round(q_norm * QUALITY_WEIGHT + traj * TRAJECTORY_WEIGHT, 4), True


# ---------- Grader OSR shape normalisation ----------------------------------

class ShapeError(Exception):
    """Raised when grader scores payload cannot be normalised."""


def _iter_score_entries(blind_scores):
    """Yield raw score entries from either legacy (camelCase list) or
    current (snake_case per_task) shapes. Raises ShapeError if neither.
    """
    if isinstance(blind_scores, list):
        # Legacy: list of {taskId, winner, outputA, outputB, ...}
        for entry in blind_scores:
            yield ("camel", entry)
        return
    if isinstance(blind_scores, dict):
        # Current: {iteration, grader_id, per_task: [...]}
        if "per_task" in blind_scores and isinstance(blind_scores["per_task"], list):
            for entry in blind_scores["per_task"]:
                yield ("snake", entry)
            return
        # Legacy-dict: {tasks: [...]} or {scores: [...]}
        for key in ("tasks", "scores"):
            val = blind_scores.get(key)
            if isinstance(val, list):
                for entry in val:
                    yield ("camel", entry)
                return
    raise ShapeError(
        "grader scores payload has neither top-level list nor a recognised "
        "dict key (per_task / tasks / scores). Cannot normalise."
    )


def _normalise_entry(shape, entry):
    """Return a dict with the common keys:
    task_id, winner, quality_a, quality_b, reasoning, feedback, suggestion.
    """
    if shape == "snake":
        return {
            "task_id": entry["task_id"],
            "winner": entry.get("winner", "TIE"),
            "quality_a": float(entry["quality_a"]),
            "quality_b": float(entry["quality_b"]),
            "reasoning": entry.get("reasoning", ""),
            "feedback": entry.get("feedback", ""),
            "suggestion": entry.get("suggestion", ""),
        }
    # camelCase
    out_a = entry.get("outputA", {})
    out_b = entry.get("outputB", {})
    q_a = out_a.get("quality", out_a.get("llmJudgeScore", 3.0))
    q_b = out_b.get("quality", out_b.get("llmJudgeScore", 3.0))
    return {
        "task_id": entry.get("taskId") or entry.get("task_id"),
        "winner": entry.get("winner", "TIE"),
        "quality_a": float(q_a),
        "quality_b": float(q_b),
        "reasoning": entry.get("reasoning", ""),
        "feedback": entry.get("feedback", ""),
        "suggestion": entry.get("suggestion", ""),
    }


def normalise_scores(blind_scores):
    """Convert either legacy or current shape into a list of common dicts.
    Raises ShapeError if the payload cannot be parsed or is empty.
    """
    normalised = []
    for shape, entry in _iter_score_entries(blind_scores):
        try:
            normalised.append(_normalise_entry(shape, entry))
        except (KeyError, TypeError, ValueError) as e:
            raise ShapeError(
                f"failed to normalise grader entry (shape={shape}): {e}; "
                f"entry keys={list(entry.keys()) if isinstance(entry, dict) else type(entry)}"
            ) from e
    if not normalised:
        raise ShapeError(
            "grader scores payload parsed to zero entries. "
            "Check the file is not empty and that its shape matches either "
            "[{taskId, outputA, outputB, winner}, ...] or "
            "{per_task: [{task_id, quality_a, quality_b, winner}, ...]}."
        )
    return normalised


# ---------- Super-Runner aggregate detection --------------------------------

def detect_trajectory_aggregated(eval_results):
    """If every task shares the same (with, without) tool-count pair AND
    there are >1 tasks, the counts are session aggregates, not per-task.
    Returns True if detected.
    """
    tasks = eval_results.get("tasks", [])
    if len(tasks) <= 1:
        return False
    pairs = set()
    for t in tasks:
        tw = t.get("withSkill", {}).get("toolUseCount")
        tu = t.get("withoutSkill", {}).get("toolUseCount")
        pairs.add((tw, tu))
    return len(pairs) == 1


# ---------- Main -------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/deblind_and_score.py <workspace> <iteration>"
              " [--cost <usd>]", file=sys.stderr)
        sys.exit(1)

    workspace = Path(sys.argv[1])
    iteration = int(sys.argv[2])
    cost = None
    if "--cost" in sys.argv:
        cost = float(sys.argv[sys.argv.index("--cost") + 1])

    results_dir = workspace / "evals" / "results" / f"iter-{iteration}"

    blind_scores_path = results_dir / "blind-grader-scores.json"
    if not blind_scores_path.exists():
        blind_scores_path = results_dir / "blind-judge-scores.json"

    blind_scores = json.loads(blind_scores_path.read_text())
    mapping = json.loads((results_dir / "blind-mapping.json").read_text())
    eval_results = json.loads((results_dir / "eval-results.json").read_text())

    mapping_by_task = {m["taskId"]: m["aIsWithSkill"] for m in mapping}
    eval_by_task = {t["taskId"]: t for t in eval_results["tasks"]}

    # Normalise grader output — raises ShapeError on mismatch/empty
    try:
        scores_list = normalise_scores(blind_scores)
    except ShapeError as e:
        print(f"ERROR: grader scores shape mismatch at {blind_scores_path}:",
              file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        print("  Scorer supports two shapes:", file=sys.stderr)
        print("    - list of {taskId, outputA.quality, outputB.quality, winner}", file=sys.stderr)
        print("    - dict with per_task: [{task_id, quality_a, quality_b, winner}, ...]", file=sys.stderr)
        sys.exit(2)

    # Super-Runner detection — if tool counts are session aggregates,
    # trajectory is unavailable per-task; don't pretend otherwise.
    trajectory_aggregated = detect_trajectory_aggregated(eval_results)

    grader_scores = []
    composites = []
    trajectory_available_count = 0

    for score in scores_list:
        task_id = score["task_id"]
        if task_id not in mapping_by_task:
            print(f"WARN: task_id {task_id} in grader scores not found in blind-mapping; skipping",
                  file=sys.stderr)
            continue
        if task_id not in eval_by_task:
            print(f"WARN: task_id {task_id} in grader scores not found in eval-results; skipping",
                  file=sys.stderr)
            continue

        a_is_with = mapping_by_task[task_id]
        eval_task = eval_by_task[task_id]

        winner = score["winner"]
        skill_was_output = "A" if a_is_with else "B"

        if winner == "TIE":
            skill_won = "tie"
        elif winner == skill_was_output:
            skill_won = "yes"
        else:
            skill_won = "no"

        tc_with = eval_task.get("withSkill", {}).get("toolUseCount")
        tc_without = eval_task.get("withoutSkill", {}).get("toolUseCount")

        quality = score["quality_a"] if a_is_with else score["quality_b"]

        # If super-runner aggregate, force trajectory to None (honesty).
        if trajectory_aggregated:
            tc_with_for_traj = None
            tc_without_for_traj = None
        else:
            tc_with_for_traj = tc_with
            tc_without_for_traj = tc_without

        comp, traj_avail = compute_composite(
            quality, tc_with_for_traj, tc_without_for_traj
        )
        if traj_avail:
            trajectory_available_count += 1
        composites.append(comp)

        grader_scores.append({
            "taskId": task_id,
            "composite": comp,
            "quality": quality,
            "trajectoryAvailable": traj_avail,
            "trajectoryEfficiency": {
                "toolCallsWith": tc_with,
                "toolCallsWithout": tc_without,
            },
            "blindComparison": {
                "winner": winner,
                "skillWasOutput": skill_was_output,
                "skillWon": skill_won,
                "reasoning": score.get("reasoning", ""),
            },
            "feedback": score.get("feedback", ""),
            "suggestion": score.get("suggestion", ""),
        })

    if not grader_scores:
        print("ERROR: after normalisation, zero tasks scored (all lookups failed). "
              "Check that blind-mapping.json + eval-results.json share task ids with grader scores.",
              file=sys.stderr)
        sys.exit(3)

    (results_dir / "grader-scores.json").write_text(
        json.dumps(grader_scores, indent=2, ensure_ascii=False))

    avg_composite = round(sum(composites) / len(composites), 4) \
        if composites else 0

    # --- Regression detection (TWO axes: composite + quality) ---
    composite_regressions = []
    quality_regressions = []
    prev_composite_map = {}
    prev_quality_map = {}
    if iteration > 1:
        prev_path = workspace / "evals" / "results" / \
            f"iter-{iteration - 1}" / "grader-scores.json"
        if not prev_path.exists():
            prev_path = workspace / "evals" / "results" / \
                f"iter-{iteration - 1}" / "judge-scores.json"
        if prev_path.exists():
            prev_data = json.loads(prev_path.read_text())
            prev_composite_map = {s["taskId"]: s.get("composite", 0)
                                  for s in prev_data}
            prev_quality_map = {s["taskId"]: s.get("quality", 0)
                                for s in prev_data}
            for s in grader_scores:
                tid = s["taskId"]
                if tid in prev_composite_map:
                    c_delta = s["composite"] - prev_composite_map[tid]
                    if c_delta < -0.05:
                        composite_regressions.append({
                            "taskId": tid,
                            "delta": round(c_delta, 4),
                        })
                if tid in prev_quality_map:
                    q_delta = s["quality"] - prev_quality_map[tid]
                    if q_delta < -0.05:
                        quality_regressions.append({
                            "taskId": tid,
                            "delta": round(q_delta, 4),
                        })

    # --- Build compact summary ---
    per_task = []
    for s in grader_scores:
        tid = s["taskId"]
        prev_c = prev_composite_map.get(tid)
        prev_q = prev_quality_map.get(tid)
        per_task.append({
            "taskId": tid,
            "composite": s["composite"],
            "quality": s["quality"],
            "skillWon": s["blindComparison"]["skillWon"],
            "trajectoryAvailable": s["trajectoryAvailable"],
            "composite_delta": round(s["composite"] - prev_c, 4) if prev_c is not None else None,
            "quality_delta": round(s["quality"] - prev_q, 4) if prev_q is not None else None,
            "feedback": (s["feedback"] or "")[:200],
            "suggestion": (s["suggestion"] or "")[:200],
        })

    weakest = sorted(per_task, key=lambda x: x["composite"])[:2]

    # --- Call convergence.py ---
    conv_cmd = [
        sys.executable,
        str(Path(__file__).parent / "convergence.py"),
        str(workspace), str(avg_composite),
    ]
    if cost is not None:
        conv_cmd += ["--cost", str(cost)]

    conv_result = subprocess.run(conv_cmd, capture_output=True, text=True)
    conv_output = {}
    try:
        conv_output = json.loads(conv_result.stdout)
    except (json.JSONDecodeError, ValueError):
        if conv_result.returncode != 0:
            print(f"WARN: convergence.py exited {conv_result.returncode}: {conv_result.stderr}",
                  file=sys.stderr)

    converged = conv_output.get("converged", False)
    reason = conv_output.get("reason", "continuing")

    trajectory_available_ratio = (
        trajectory_available_count / len(grader_scores) if grader_scores else 0
    )

    summary = {
        "iteration": iteration,
        "composite_score": avg_composite,
        "converged": converged,
        "convergence_reason": reason,
        "trajectory_degraded": trajectory_aggregated,
        "trajectory_available_ratio": round(trajectory_available_ratio, 3),
        "composite_regressions": composite_regressions,
        "quality_regressions": quality_regressions,
        "regressions": composite_regressions,  # back-compat alias
        "weakest_tasks": [t["taskId"] for t in weakest],
        "per_task": per_task,
    }

    # Rollback-policy hint: quality regressions are safety-critical;
    # composite-only regressions with quality gains are trajectory noise.
    if quality_regressions:
        summary["rollback_recommendation"] = "quality_regressed"
    elif composite_regressions and not quality_regressions:
        summary["rollback_recommendation"] = "trajectory_regression_observed_do_not_rollback"
    else:
        summary["rollback_recommendation"] = "none"

    (results_dir / "iteration-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False))

    # --- v2: OSR Grader Digest (compact, schema-validated subset) ---
    digest = {
        "iteration": iteration,
        "composite_score": avg_composite,
        "converged": converged,
        "convergence_reason": reason,
        "trajectory_degraded": trajectory_aggregated,
        "trajectory_available_ratio": round(trajectory_available_ratio, 3),
        "scores_file": str(results_dir / "grader-scores.json"),
        "per_task": [
            {
                "task_id": s["taskId"],
                "composite": s["composite"],
                "quality": s["quality"],
                "winner": s["blindComparison"]["winner"],
                "skill_won": s["blindComparison"]["skillWon"],
                "tool_count_with": s["trajectoryEfficiency"]["toolCallsWith"],
                "tool_count_without": s["trajectoryEfficiency"]["toolCallsWithout"],
                "trajectory_available": s["trajectoryAvailable"],
                "feedback_ref": {
                    "file": str(results_dir / "grader-scores.json"),
                    "task_id": s["taskId"],
                },
            }
            for s in grader_scores
        ],
        "aggregate": {
            "winner_dist": {
                "A": sum(1 for s in grader_scores
                         if s["blindComparison"]["winner"] == "A"),
                "B": sum(1 for s in grader_scores
                         if s["blindComparison"]["winner"] == "B"),
                "TIE": sum(1 for s in grader_scores
                           if s["blindComparison"]["winner"] == "TIE"),
            },
            "skill_won_rate": round(
                sum(1 for s in grader_scores
                    if s["blindComparison"]["skillWon"] == "yes")
                / max(1, len(grader_scores)), 3),
            "quality_range": [
                min((s["quality"] for s in grader_scores), default=0),
                max((s["quality"] for s in grader_scores), default=0),
            ],
        },
        "composite_regressions": composite_regressions,
        "quality_regressions": quality_regressions,
        "regressions": composite_regressions,  # back-compat
        "rollback_recommendation": summary["rollback_recommendation"],
        "weakest_tasks": [t["taskId"] for t in weakest],
    }
    digest_path = results_dir / "osr-grader-digest.json"
    digest_path.write_text(json.dumps(digest, indent=2, ensure_ascii=False))

    # --- v2: Append event to state/events.jsonl (non-blocking) ---
    state_mgr = Path(__file__).parent / "state_manager.py"
    if (workspace / "state.json").exists() and state_mgr.exists():
        summary_text = (f"iter-{iteration} composite={avg_composite} "
                        f"reason={reason}")[:120]
        try:
            subprocess.run(
                [sys.executable, str(state_mgr), str(workspace),
                 "append-event",
                 "--event-type", "snapshot_created",
                 "--iter", str(iteration),
                 "--ref", str(digest_path),
                 "--summary", summary_text],
                capture_output=True, timeout=10,
            )
            subprocess.run(
                [sys.executable, str(state_mgr), str(workspace),
                 "append-score",
                 "--iter", str(iteration),
                 "--composite", str(avg_composite)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass  # scoring completes even if state infra is absent

    # --- Compact stdout for Orchestrator ---
    print(f"composite={avg_composite} converged={converged} reason={reason}")
    if trajectory_aggregated:
        print("WARN: trajectory_degraded=true (super-runner aggregate detected; composite is quality-only)")
    if composite_regressions:
        reg_ids = ", ".join(r["taskId"] for r in composite_regressions)
        print(f"COMPOSITE REGRESSIONS: {reg_ids}")
    if quality_regressions:
        reg_ids = ", ".join(r["taskId"] for r in quality_regressions)
        print(f"QUALITY REGRESSIONS (rollback-eligible): {reg_ids}")
    print(f"rollback_recommendation: {summary['rollback_recommendation']}")
    weak_ids = ", ".join(t["taskId"] for t in weakest)
    print(f"weakest: {weak_ids}")
    print(f"details: {results_dir / 'iteration-summary.json'}")
    print(f"osr_digest: {digest_path}")


if __name__ == "__main__":
    main()
