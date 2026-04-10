#!/usr/bin/env python3
"""De-blind judge scores, compute composite, check convergence.

Reads blind-judge-scores.json + blind-mapping.json + eval-results.json.
Writes judge-scores.json + iteration-summary.json.
Calls convergence.py internally.

Usage:
    python scripts/deblind_and_score.py <workspace> <iteration> [--cost <usd>]

stdout prints a compact 3-line summary for the Orchestrator.
"""

import json
import subprocess
import sys
from pathlib import Path


def compute_trajectory(tool_calls_with, tool_calls_without):
    if tool_calls_without <= 0:
        return 0
    return max(0, 1 - tool_calls_with / tool_calls_without)


def compute_composite(execution_pass, assertion_coverage, llm_judge_score,
                      tool_calls_with, tool_calls_without, task_type=None):
    trajectory = compute_trajectory(tool_calls_with, tool_calls_without)

    if task_type in ("reasoning", "transfer"):
        return round(
            (llm_judge_score / 5) * 0.55
            + trajectory * 0.45,
            4
        )

    exec_val = 1.0 if execution_pass else 0.0
    return round(
        exec_val * 0.1
        + assertion_coverage * 0.15
        + (llm_judge_score / 5) * 0.40
        + trajectory * 0.35,
        4
    )


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

    blind_scores = json.loads((results_dir / "blind-judge-scores.json").read_text())
    mapping = json.loads((results_dir / "blind-mapping.json").read_text())
    eval_results = json.loads((results_dir / "eval-results.json").read_text())

    eval_tasks_path = workspace / "evals" / "eval-tasks.json"
    task_types = {}
    if eval_tasks_path.exists():
        for t in json.loads(eval_tasks_path.read_text()):
            task_types[t["id"]] = t.get("type", "coding")

    mapping_by_task = {m["taskId"]: m["aIsWithSkill"] for m in mapping}
    eval_by_task = {t["taskId"]: t for t in eval_results["tasks"]}

    scores_list = blind_scores if isinstance(blind_scores, list) \
        else blind_scores.get("tasks", blind_scores.get("scores", []))

    judge_scores = []
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

        tt = task_types.get(task_id, score.get("taskType", "coding"))

        if tt in ("reasoning", "transfer"):
            ep = None
            ac = None
        else:
            if "executionPass" not in skill_output:
                print(f"WARNING: {task_id} missing executionPass, defaulting to False",
                      file=sys.stderr)
            if "llmJudgeScore" not in skill_output:
                print(f"WARNING: {task_id} missing llmJudgeScore, defaulting to 3.0",
                      file=sys.stderr)
            ep = skill_output.get("executionPass", False)
            ac = skill_output.get("assertionCoverage", 0.0)

        ljs = skill_output.get("llmJudgeScore", 3.0)
        na = skill_output.get("novelApplicability", None)

        comp = compute_composite(
            ep if ep is not None else False,
            ac if ac is not None else 0.0,
            ljs, tc_with, tc_without, task_type=tt
        )
        composites.append(comp)

        judge_scores.append({
            "taskId": task_id,
            "taskType": tt,
            "composite": comp,
            "channels": {
                "executionPass": ep,
                "assertionCoverage": ac,
                "llmJudgeScore": ljs,
                "novelApplicability": na,
                "trajectoryEfficiency": {
                    "toolCallsWith": tc_with,
                    "toolCallsWithout": tc_without,
                },
            },
            "blindComparison": {
                "winner": winner,
                "skillWasOutput": skill_was_output,
                "skillWon": skill_won,
                "reasoning": score.get("reasoning", ""),
            },
            "feedback": score.get("feedback", ""),
            "suggestion": score.get("suggestion", ""),
            "evalFeedback": score.get("evalFeedback", {}),
        })

    (results_dir / "judge-scores.json").write_text(
        json.dumps(judge_scores, indent=2, ensure_ascii=False))

    avg_composite = round(sum(composites) / len(composites), 4) \
        if composites else 0

    # --- Knowledge density (exclude index SKILL.md) ---
    skill_line_count = 0
    skills_dir = workspace / "skills"
    if skills_dir.exists():
        for md_file in skills_dir.rglob("SKILL.md"):
            if md_file.parent.name == "index":
                continue
            skill_line_count += len(md_file.read_text().splitlines())
    knowledge_density = round(avg_composite / skill_line_count * 100, 4) \
        if skill_line_count > 0 else None

    # --- Regression detection ---
    regressions = []
    prev_scores_map = {}
    if iteration > 1:
        prev_path = workspace / "evals" / "results" / \
            f"iter-{iteration - 1}" / "judge-scores.json"
        if prev_path.exists():
            prev_data = json.loads(prev_path.read_text())
            prev_scores_map = {s["taskId"]: s.get("composite", 0)
                               for s in prev_data}
            for s in judge_scores:
                tid = s["taskId"]
                if tid in prev_scores_map:
                    delta = s["composite"] - prev_scores_map[tid]
                    if delta < -0.05:
                        regressions.append({"taskId": tid,
                                            "delta": round(delta, 4)})

    # --- Build compact summary ---
    per_task = []
    for s in judge_scores:
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
        "knowledge_density": knowledge_density,
        "skill_line_count": skill_line_count,
        "converged": converged,
        "convergence_reason": reason,
        "regressions": regressions,
        "weakest_tasks": [t["taskId"] for t in weakest],
        "per_task": per_task,
    }

    (results_dir / "iteration-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False))

    # --- Compact stdout for Orchestrator (< 200 chars) ---
    print(f"composite={avg_composite} converged={converged} reason={reason}")
    if regressions:
        reg_ids = ", ".join(r["taskId"] for r in regressions)
        print(f"REGRESSIONS: {reg_ids}")
    weak_ids = ", ".join(t["taskId"] for t in weakest)
    print(f"weakest: {weak_ids}")
    print(f"details: {results_dir / 'iteration-summary.json'}")


if __name__ == "__main__":
    main()
