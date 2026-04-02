#!/usr/bin/env python3
"""Convergence check + results.tsv append.

Usage:
    python scripts/convergence.py <workspace> <score> [--cost <usd>] [--status keep|discard] [--desc "text"]

Reads workspace/results.tsv history, appends the new score (and optional
incremental cost), checks ε-δ convergence, and prints a JSON summary to stdout.
"""

import argparse
import json
import sys
from pathlib import Path

EPSILON = 0.03
STABLE_ROUNDS = 2
MIN_SCORE = 0.6
MAX_BUDGET_USD = 30.0
MAX_ITERATIONS = 10


def main():
    parser = argparse.ArgumentParser(description="Convergence check + results.tsv append")
    parser.add_argument("workspace", type=Path, help="Path to workspace directory")
    parser.add_argument("score", type=float, help="Composite score for this iteration")
    parser.add_argument("--cost", type=float, default=0.0,
                        help="Incremental cost (USD) for this iteration, added to running total")
    parser.add_argument("--status", choices=["keep", "discard"], default=None,
                        help="Override auto-detected status")
    parser.add_argument("--desc", type=str, default=None, help="Description for this iteration")
    args = parser.parse_args()

    ws = args.workspace
    score = args.score
    tsv_path = ws / "results.tsv"

    # Parse existing scores from results.tsv
    scores = []
    iteration = 0
    total_cost = 0.0
    if tsv_path.exists():
        for line in tsv_path.read_text().strip().split("\n")[1:]:  # skip header
            parts = line.split("\t")
            if len(parts) >= 3:
                scores.append(float(parts[1]))
                iteration = int(parts[0])
                total_cost = float(parts[2])

    iteration += 1
    scores.append(score)
    total_cost += args.cost

    # Compute deltas
    delta = score - scores[-2] if len(scores) >= 2 else None
    prev_delta = scores[-2] - scores[-3] if len(scores) >= 3 else None
    delta_delta = delta - prev_delta if delta is not None and prev_delta is not None else None

    # Convergence checks — require STABLE_ROUNDS consecutive small deltas
    recent_deltas = []
    for i in range(max(0, len(scores) - STABLE_ROUNDS - 1), len(scores)):
        if i > 0:
            recent_deltas.append(scores[i] - scores[i - 1])

    all_small = (
        len(recent_deltas) >= STABLE_ROUNDS
        and all(abs(d) < EPSILON for d in recent_deltas[-STABLE_ROUNDS:])
    )
    decelerating = (
        delta_delta is not None
        and delta_delta < 0
        and delta is not None
        and abs(delta) < EPSILON
    )
    above_minimum = score >= MIN_SCORE
    budget_exhausted = total_cost >= MAX_BUDGET_USD
    max_iters_reached = iteration >= MAX_ITERATIONS

    converged = (above_minimum and (all_small or decelerating)) or budget_exhausted or max_iters_reached

    # Determine status for results.tsv
    auto_status = "discard" if delta is not None and delta < -0.05 else "keep"
    log_status = args.status or auto_status
    log_desc = args.desc or f"iteration {iteration}"

    # Append to results.tsv
    if not tsv_path.exists():
        tsv_path.write_text("iteration\tcomposite\tcost_usd\tstatus\tdescription\n")
    with open(tsv_path, "a") as f:
        f.write(f"{iteration}\t{score:.4f}\t{total_cost:.2f}\t{log_status}\t{log_desc}\n")

    # Determine reason
    if budget_exhausted:
        reason = "budget_exhausted"
    elif max_iters_reached:
        reason = "max_iterations"
    elif converged:
        reason = "score_converged"
    else:
        reason = "continuing"

    output = {
        "converged": converged,
        "reason": reason,
        "iteration": iteration,
        "score": score,
        "delta": delta,
        "deltaDelta": delta_delta,
        "totalCostUsd": total_cost,
        "scoresHistory": scores,
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
