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
"""

import json
import subprocess
import sys
from pathlib import Path


def compute_trajectory(tool_calls_with, tool_calls_without):
    if tool_calls_without <= 0:
        return 0
    return max(0, 1 - tool_calls_with / tool_calls_without)


def compute_composite(quality, tool_calls_with, tool_calls_without):
    trajectory = compute_trajectory(tool_calls_with, tool_calls_without)
    return round((quality / 5) * 0.6 + trajectory * 0.4, 4)


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

    scores_list = blind_scores if isinstance(blind_scores, list) \
        else blind_scores.get("tasks", blind_scores.get("scores", []))

    grader_scores = []
    composites = []

    for score in scores_list:
        task_id = score["taskId"]
        a_is_with = mapping_by_task[task_id]
        eval_task = eval_by_task[task_id]

        skill_output = score["outputA"] if a_is_with else score["outputB"]

        winner = score.get("winner", "TIE")
        skill_was_output = "A" if a_is_with else "B"

        if winner == "TIE":
            skill_won = "tie"
        elif winner == skill_was_output:
            skill_won = "yes"
        else:
            skill_won = "no"

        tc_with = eval_task["withSkill"].get("toolUseCount", 0)
        tc_without = eval_task["withoutSkill"].get("toolUseCount", 0)

        quality = skill_output.get("quality",
                                   skill_output.get("llmJudgeScore", 3.0))

        comp = compute_composite(quality, tc_with, tc_without)
        composites.append(comp)

        grader_scores.append({
            "taskId": task_id,
            "composite": comp,
            "quality": quality,
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

    (results_dir / "grader-scores.json").write_text(
        json.dumps(grader_scores, indent=2, ensure_ascii=False))

    avg_composite = round(sum(composites) / len(composites), 4) \
        if composites else 0

    # --- Regression detection ---
    regressions = []
    prev_scores_map = {}
    if iteration > 1:
        prev_path = workspace / "evals" / "results" / \
            f"iter-{iteration - 1}" / "grader-scores.json"
        if not prev_path.exists():
            prev_path = workspace / "evals" / "results" / \
                f"iter-{iteration - 1}" / "judge-scores.json"
        if prev_path.exists():
            prev_data = json.loads(prev_path.read_text())
            prev_scores_map = {s["taskId"]: s.get("composite", 0)
                               for s in prev_data}
            for s in grader_scores:
                tid = s["taskId"]
                if tid in prev_scores_map:
                    delta = s["composite"] - prev_scores_map[tid]
                    if delta < -0.05:
                        regressions.append({"taskId": tid,
                                            "delta": round(delta, 4)})

    # --- Build compact summary ---
    per_task = []
    for s in grader_scores:
        tid = s["taskId"]
        prev = prev_scores_map.get(tid)
        per_task.append({
            "taskId": tid,
            "composite": s["composite"],
            "skillWon": s["blindComparison"]["skillWon"],
            "delta": round(s["composite"] - prev, 4) if prev is not None
                     else None,
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
        pass

    converged = conv_output.get("converged", False)
    reason = conv_output.get("reason", "continuing")

    summary = {
        "iteration": iteration,
        "composite_score": avg_composite,
        "converged": converged,
        "convergence_reason": reason,
        "regressions": regressions,
        "weakest_tasks": [t["taskId"] for t in weakest],
        "per_task": per_task,
    }

    (results_dir / "iteration-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False))

    # --- v2: OSR Grader Digest (compact, schema-validated subset) ---
    # This is what the Orchestrator consumes without reading the full grader-
    # scores.json; references long text by path so L3 content never enters L1.
    digest = {
        "iteration": iteration,
        "composite_score": avg_composite,
        "converged": converged,
        "convergence_reason": reason,
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
        "regressions": regressions,
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
            # Also register the composite into scores_history for Markov recovery
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
    if regressions:
        reg_ids = ", ".join(r["taskId"] for r in regressions)
        print(f"REGRESSIONS: {reg_ids}")
    weak_ids = ", ".join(t["taskId"] for t in weakest)
    print(f"weakest: {weak_ids}")
    print(f"details: {results_dir / 'iteration-summary.json'}")
    print(f"osr_digest: {digest_path}")


if __name__ == "__main__":
    main()
