#!/usr/bin/env python3
"""Generate an eval viewer HTML from workspace data.

Usage:
    python scripts/gen_viewer.py <workspace> [iteration]

Reads eval-tasks.json, eval-results.json, grader-scores.json from the
workspace and populates the viewer template. Opens the result in browser.
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
TEMPLATE_PATH = PROJECT_ROOT / "assets" / "viewer-template.html"


def load_json(path: Path):
    if path.exists():
        return json.loads(path.read_text())
    return None


def gather_history(ws: Path, up_to_iter: int) -> list:
    history = []
    for i in range(1, up_to_iter + 1):
        scores_path = ws / "evals" / "results" / f"iter-{i}" / "grader-scores.json"
        if not scores_path.exists():
            scores_path = ws / "evals" / "results" / f"iter-{i}" / "judge-scores.json"
        try:
            scores = json.loads(scores_path.read_text())
            composites = []
            for s in scores:
                comp = s.get("composite", 0)
                if comp == 0:
                    quality = s.get("quality", s.get("channels", {}).get("llmJudgeScore", 3.0))
                    traj_info = s.get("trajectoryEfficiency", s.get("channels", {}).get("trajectoryEfficiency", {}))
                    tc_with = traj_info.get("toolCallsWith", 0)
                    tc_without = traj_info.get("toolCallsWithout", 1)
                    traj_score = max(0, 1 - tc_with / tc_without) if tc_without > 0 else 0
                    comp = (quality / 5) * 0.6 + traj_score * 0.4
                composites.append(comp)
            avg = sum(composites) / len(composites) if composites else 0
            history.append({"iteration": i, "score": avg})
        except (FileNotFoundError, json.JSONDecodeError, ZeroDivisionError):
            pass
    return history


def main():
    if len(sys.argv) < 2:
        print("Usage: gen_viewer.py <workspace> [iteration]", file=sys.stderr)
        sys.exit(1)

    ws = Path(sys.argv[1])
    iteration = int(sys.argv[2]) if len(sys.argv) > 2 else None

    # Auto-detect iteration from results.tsv if not specified
    if iteration is None:
        tsv_path = ws / "results.tsv"
        if tsv_path.exists():
            lines = [l for l in tsv_path.read_text().strip().split("\n")[1:] if l.strip()]
            iteration = int(lines[-1].split("\t")[0]) if lines else 1
        else:
            iteration = 1

    iter_dir = ws / "evals" / "results" / f"iter-{iteration}"

    eval_tasks = load_json(ws / "evals" / "eval-tasks.json") or []
    eval_results = load_json(iter_dir / "eval-results.json")
    grader_scores = load_json(iter_dir / "grader-scores.json") \
        or load_json(iter_dir / "judge-scores.json") or []
    blind_mapping = load_json(iter_dir / "blind-mapping.json") or {}

    # Read results.tsv for cost
    total_cost = 0.0
    tsv_path = ws / "results.tsv"
    if tsv_path.exists():
        for line in tsv_path.read_text().strip().split("\n")[1:]:
            parts = line.split("\t")
            if len(parts) >= 3:
                total_cost = float(parts[2])

    history = gather_history(ws, iteration)
    scores_list = [h["score"] for h in history]

    viewer_data = {
        "repoUrl": "",
        "iteration": iteration,
        "totalCostUsd": total_cost,
        "convergence": {"scores": scores_list, "converged": False},
        "evalTasks": eval_tasks,
        "evalResults": eval_results.get("tasks", []) if eval_results else [],
        "judgeScores": grader_scores,
        "blindMapping": blind_mapping,
        "history": history,
    }

    if not TEMPLATE_PATH.exists():
        print(f"Template not found: {TEMPLATE_PATH}", file=sys.stderr)
        sys.exit(1)

    template = TEMPLATE_PATH.read_text()
    html = template.replace("__DATA_PLACEHOLDER__", json.dumps(viewer_data, ensure_ascii=False))

    output_path = iter_dir / "viewer.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)

    # Open in browser
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(output_path)], check=True)
        elif system == "Windows":
            os.startfile(str(output_path))
        else:
            subprocess.run(["xdg-open", str(output_path)], check=True)
    except Exception:
        print(f"Could not open browser. View manually: {output_path}", file=sys.stderr)

    print(json.dumps({
        "status": "viewer_generated",
        "path": str(output_path),
        "iteration": iteration,
        "tasksCount": len(eval_tasks),
    }, indent=2))


if __name__ == "__main__":
    main()
