#!/usr/bin/env python3
"""Blind eval results for grader review.

Reads eval-results.json, randomly assigns A/B labels,
writes blinded-eval-results.json and blind-mapping.json.

Usage:
    python scripts/blind_eval.py <workspace> <iteration>
"""

import json
import random
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/blind_eval.py <workspace> <iteration>",
              file=sys.stderr)
        sys.exit(1)

    workspace = Path(sys.argv[1])
    iteration = int(sys.argv[2])

    results_dir = workspace / "evals" / "results" / f"iter-{iteration}"
    eval_results_path = results_dir / "eval-results.json"

    if not eval_results_path.exists():
        print(f"ERROR: {eval_results_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(eval_results_path) as f:
        eval_results = json.load(f)

    blinded_tasks = []
    mapping = []

    for task in eval_results["tasks"]:
        a_is_with = random.random() > 0.5

        if a_is_with:
            output_a = task["withSkill"]
            output_b = task["withoutSkill"]
        else:
            output_a = task["withoutSkill"]
            output_b = task["withSkill"]

        blinded_tasks.append({
            "taskId": task["taskId"],
            "outputA": output_a,
            "outputB": output_b,
        })

        mapping.append({
            "taskId": task["taskId"],
            "aIsWithSkill": a_is_with,
        })

    blinded_path = results_dir / "blinded-eval-results.json"
    mapping_path = results_dir / "blind-mapping.json"

    with open(blinded_path, "w") as f:
        json.dump({"iteration": iteration, "tasks": blinded_tasks},
                  f, indent=2, ensure_ascii=False)

    with open(mapping_path, "w") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

    print(f"blinded {len(blinded_tasks)} tasks")
    print(f"output: {blinded_path}")
    print(f"mapping: {mapping_path}")


if __name__ == "__main__":
    main()
