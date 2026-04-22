#!/usr/bin/env python3
"""Aggregate K grader ensemble outputs into a single blind-grader-scores.json.

Each Grader in the ensemble runs in its own physically isolated worktree and
produces:
    - <grader_work>/blind-grader-scores.json  (long-form, per-task quality/winner/
      reasoning/feedback/suggestion)
    - <grader_work>/osr.json                  (short-form OSR, schema-validated)

This script collapses K parallel grader outputs into:
    - blind-grader-scores.json  (K=1 compatible; consumed by deblind_and_score.py
      unchanged): majority vote on winner, median quality, concatenated feedback.
    - ensemble-metrics.json     (agreement metrics; disagreement_tasks list used
      by invariant_check as anomalies signal).

With K=1 this degenerates to a copy — the downstream pipeline is identical to
the pre-ensemble flow.

Aggregation rules
-----------------
* `winner`:    majority vote across graders. Ties → "TIE". (K=2 split → TIE
               unless |median(quality_a) - median(quality_b)| > 0.5.)
* `quality_*`: median across graders (robust to a single outlier evaluator).
* `reasoning`: concatenated with per-grader tags `[g1] ... [g2] ...`.
* `feedback`:  same (Skill Writer benefits from seeing multiple perspectives).
* `suggestion`: same.
* `tool_count_*`: passed through — the Orchestrator injects these identically
                  into every grader's input, so they must all agree; we assert
                  this and record any mismatch as an anomaly.

Disagreement metrics
--------------------
* `winner_agreement`:  for each task, fraction of graders picking the majority
                       winner. Mean across tasks reported as overall.
* `quality_stdev`:     stdev of quality_a and quality_b per task, averaged.
* `disagreement_tasks`: tasks where winner is split OR stdev > 1.0 OR two
                       graders disagree by ≥ 2 on quality. These surface up to
                       the Orchestrator (via invariant_check) as anomalies.

Usage
-----
    python3 scripts/aggregate_grades.py \\
        --workspace workspace/ --iter 3 \\
        --grader-dirs workspace/.worktrees/grader-iter3-g1,\\
                      workspace/.worktrees/grader-iter3-g2,\\
                      workspace/.worktrees/grader-iter3-g3

The script is deterministic given the inputs (same grader files → same output).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path


# ---- I/O -------------------------------------------------------------------


def _read_grader_scores(grader_dir: Path) -> tuple[list[dict], dict | None]:
    """Return (scores_list, osr_dict) from one grader worktree.

    Accepts either `blind-grader-scores.json` (preferred) or the legacy
    `blind-judge-scores.json`. scores_list is normalized to a list of task
    dicts regardless of whether the file stored {tasks: [...]}, {scores: [...]},
    or a bare list.
    """
    scores_path = grader_dir / "blind-grader-scores.json"
    if not scores_path.exists():
        scores_path = grader_dir / "blind-judge-scores.json"
    if not scores_path.exists():
        raise FileNotFoundError(
            f"no blind-grader-scores.json in {grader_dir}")

    raw = json.loads(scores_path.read_text())
    if isinstance(raw, list):
        scores = raw
    elif isinstance(raw, dict):
        scores = raw.get("tasks") or raw.get("scores") or []
    else:
        raise ValueError(f"unexpected scores shape in {scores_path}")

    osr_path = grader_dir / "osr.json"
    osr = json.loads(osr_path.read_text()) if osr_path.exists() else None
    return scores, osr


def _index_by_task(scores: list[dict]) -> dict[str, dict]:
    return {s["taskId"]: s for s in scores}


# ---- Aggregation primitives -----------------------------------------------


def _majority_winner(winners: list[str], quality_delta: float,
                     tiebreak_threshold: float = 0.5) -> tuple[str, float]:
    """Return (winner, agreement_fraction).

    agreement_fraction = (# graders picking the chosen winner) / K.
    Ties broken by sign of quality_delta (median(quality_a) - median(quality_b))
    when |delta| > threshold; otherwise TIE.
    """
    if not winners:
        return "TIE", 0.0
    counts = Counter(winners)
    top_count = max(counts.values())
    top_winners = [w for w, c in counts.items() if c == top_count]

    if len(top_winners) == 1:
        winner = top_winners[0]
    else:
        # Tie among majority picks — use quality delta
        if quality_delta > tiebreak_threshold:
            winner = "A"
        elif quality_delta < -tiebreak_threshold:
            winner = "B"
        else:
            winner = "TIE"

    # Agreement fraction is against the finally-chosen winner (TIE counts
    # graders who picked TIE; A counts graders who picked A; etc.)
    frac = counts.get(winner, 0) / len(winners)
    return winner, round(frac, 3)


def _median(xs: list[float]) -> float:
    return round(statistics.median(xs), 3)


def _stdev_safe(xs: list[float]) -> float:
    return round(statistics.stdev(xs), 3) if len(xs) >= 2 else 0.0


def _concat_by_grader(texts: list[str], label: str = "g") -> str:
    """Concatenate K strings with per-grader labels. Skip empty/None."""
    chunks = []
    for i, t in enumerate(texts, 1):
        if not t:
            continue
        chunks.append(f"[{label}{i}] {t.strip()}")
    return "\n\n".join(chunks)


# ---- Main aggregation -----------------------------------------------------


def aggregate_task(task_id: str, per_grader: list[dict],
                   tiebreak_threshold: float) -> tuple[dict, dict]:
    """Return (aggregated_score, disagreement_record).

    per_grader: list of K task-level score dicts (one per grader) for this task.
    """
    quality_a_vals = []
    quality_b_vals = []
    winners = []
    reasonings = []
    feedbacks = []
    suggestions = []
    tool_counts_a = []
    tool_counts_b = []

    for g in per_grader:
        out_a = g.get("outputA") or {}
        out_b = g.get("outputB") or {}
        qa = out_a.get("quality", out_a.get("llmJudgeScore"))
        qb = out_b.get("quality", out_b.get("llmJudgeScore"))
        if qa is not None:
            quality_a_vals.append(float(qa))
        if qb is not None:
            quality_b_vals.append(float(qb))
        winners.append(g.get("winner", "TIE"))
        reasonings.append(g.get("reasoning", ""))
        feedbacks.append(g.get("feedback", ""))
        suggestions.append(g.get("suggestion", ""))
        # tool_count may live on outputA/B or at root; normalize
        tc_a = (out_a.get("toolUseCount")
                or g.get("tool_count_a_from_log"))
        tc_b = (out_b.get("toolUseCount")
                or g.get("tool_count_b_from_log"))
        tool_counts_a.append(tc_a)
        tool_counts_b.append(tc_b)

    median_qa = _median(quality_a_vals) if quality_a_vals else 3.0
    median_qb = _median(quality_b_vals) if quality_b_vals else 3.0
    winner, winner_agreement = _majority_winner(
        winners, median_qa - median_qb, tiebreak_threshold)

    # Tool count consistency: every grader receives the injected value, so they
    # must all agree (excluding Nones). If they diverge, record anomaly and
    # pick the modal value.
    def _consistent_tc(vals: list):
        non_null = [v for v in vals if v is not None]
        if not non_null:
            return None, False
        unique = set(non_null)
        if len(unique) == 1:
            return non_null[0], True
        return Counter(non_null).most_common(1)[0][0], False

    tc_a_final, tc_a_ok = _consistent_tc(tool_counts_a)
    tc_b_final, tc_b_ok = _consistent_tc(tool_counts_b)

    aggregated = {
        "taskId": task_id,
        "outputA": {"quality": median_qa, "toolUseCount": tc_a_final},
        "outputB": {"quality": median_qb, "toolUseCount": tc_b_final},
        "winner": winner,
        "reasoning": _concat_by_grader(reasonings),
        "feedback": _concat_by_grader(feedbacks),
        "suggestion": _concat_by_grader(suggestions),
    }

    stdev_a = _stdev_safe(quality_a_vals)
    stdev_b = _stdev_safe(quality_b_vals)
    winner_split = len(set(winners)) > 1
    large_quality_gap = any(
        (max(vs) - min(vs)) >= 2.0
        for vs in (quality_a_vals, quality_b_vals) if len(vs) >= 2
    )
    disagree = (winner_split or stdev_a > 1.0 or stdev_b > 1.0
                or large_quality_gap or not tc_a_ok or not tc_b_ok)

    disagreement_record = {
        "task_id": task_id,
        "winners": winners,
        "winner_agreement": winner_agreement,
        "quality_a_values": quality_a_vals,
        "quality_b_values": quality_b_vals,
        "quality_stdev_a": stdev_a,
        "quality_stdev_b": stdev_b,
        "tool_count_a_consistent": tc_a_ok,
        "tool_count_b_consistent": tc_b_ok,
        "flagged": disagree,
    }
    return aggregated, disagreement_record


def aggregate(grader_dirs: list[Path], results_dir: Path,
              tiebreak_threshold: float = 0.5) -> dict:
    """Write aggregated blind-grader-scores.json + ensemble-metrics.json.

    Returns a compact summary dict suitable for stdout.
    """
    k = len(grader_dirs)
    per_grader_scores = []
    osrs = []
    for d in grader_dirs:
        scores, osr = _read_grader_scores(d)
        per_grader_scores.append(_index_by_task(scores))
        osrs.append({"grader_dir": str(d), "osr": osr})

    # Discover task union; graders must all cover the same tasks but be tolerant
    all_task_ids: list[str] = []
    seen = set()
    for idx in per_grader_scores:
        for tid in idx:
            if tid not in seen:
                seen.add(tid)
                all_task_ids.append(tid)

    aggregated: list[dict] = []
    disagreements: list[dict] = []
    missing: list[dict] = []

    for tid in all_task_ids:
        per_grader = []
        for gi, idx in enumerate(per_grader_scores):
            if tid in idx:
                per_grader.append(idx[tid])
            else:
                missing.append({"task_id": tid, "grader_index": gi})
        if not per_grader:
            continue
        agg, dis = aggregate_task(tid, per_grader, tiebreak_threshold)
        aggregated.append(agg)
        disagreements.append(dis)

    # Overall agreement metrics
    mean_winner_agreement = (
        round(sum(d["winner_agreement"] for d in disagreements)
              / len(disagreements), 3)
        if disagreements else 1.0
    )
    mean_quality_stdev = (
        round(sum((d["quality_stdev_a"] + d["quality_stdev_b"]) / 2
                  for d in disagreements) / len(disagreements), 3)
        if disagreements else 0.0
    )
    disagreement_tasks = [d["task_id"] for d in disagreements if d["flagged"]]

    # Write outputs
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "blind-grader-scores.json").write_text(
        json.dumps(aggregated, indent=2, ensure_ascii=False))

    metrics = {
        "k": k,
        "grader_dirs": [str(d) for d in grader_dirs],
        "mean_winner_agreement": mean_winner_agreement,
        "mean_quality_stdev": mean_quality_stdev,
        "disagreement_tasks": disagreement_tasks,
        "per_task": disagreements,
        "missing": missing,
        "grader_osrs": osrs,
    }
    (results_dir / "ensemble-metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False))

    return {
        "k": k,
        "tasks": len(aggregated),
        "mean_winner_agreement": mean_winner_agreement,
        "mean_quality_stdev": mean_quality_stdev,
        "disagreement_tasks": disagreement_tasks,
        "missing_count": len(missing),
    }


# ---- CLI ------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(prog="aggregate_grades")
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument(
        "--grader-dirs", required=True,
        help="comma-separated list of grader worktree directories")
    ap.add_argument("--tiebreak-threshold", type=float, default=0.5,
                    help="|median(q_a) - median(q_b)| above which a winner tie "
                         "is broken by quality delta (default 0.5)")
    args = ap.parse_args()

    grader_dirs = [Path(p.strip()).resolve()
                   for p in args.grader_dirs.split(",") if p.strip()]
    if not grader_dirs:
        print("ERROR: --grader-dirs is empty", file=sys.stderr)
        return 2
    for d in grader_dirs:
        if not d.is_dir():
            print(f"ERROR: {d} is not a directory", file=sys.stderr)
            return 2

    results_dir = (args.workspace / "evals" / "results"
                   / f"iter-{args.iter}").resolve()

    try:
        summary = aggregate(grader_dirs, results_dir, args.tiebreak_threshold)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
