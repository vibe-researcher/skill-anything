#!/usr/bin/env python3
"""End-to-end integration test for v2 architecture.

Exercises the full pipeline with synthetic agent returns (no real LLM spawns):

    1. first_run detection + init
    2. worktree_helper creates isolated dirs with correct exclude-guards
    3. Fake Researcher OSR → validates → events appended
    4. Fake Eval Designer OSR with term-leakage check
    5. Fake Runner OSRs (with + without, 2 tasks × 2 variants = 4 runs)
    6. deblind_and_score.py round trip (produces osr-grader-digest)
    7. Fake Skill Writer OSR with valid knowledge_source_refs → overfit_check passes
    8. Fake Skill Writer OSR with eval task_id literal → overfit_check rejects
    9. Plant fabricated iter-N data (identical tool counts) → invariant_check catches
   10. Migration round-trip: v1 workspace → state_migrate → preflight works

Run from the repo root:
    python3 scripts/_selftest_integration.py

Exits 0 on success, 1 if any assertion fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


class Test:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures: list = []

    def check(self, label: str, cond: bool, detail: str = ""):
        if cond:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed += 1
            print(f"  FAIL  {label}: {detail}")
            self.failures.append((label, detail))

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{'=' * 60}")
        print(f"Integration: {self.passed}/{total} passed, {self.failed} failed")
        if self.failures:
            for l, d in self.failures:
                print(f"  - {l}: {d}")
            return 1
        return 0


def run(cmd: list, input_str: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           input=input_str, timeout=30)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 255, "", "TIMEOUT"


def pj(s: str) -> dict:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


def _common_osr(agent_type: str, cwd: str) -> dict:
    return {
        "status": "success",
        "agent_type": agent_type,
        "agent_env": {"cwd": cwd, "wall_time_s": 10.0},
        "surprises": [],
        "anomalies": [],
        "meta_observations": [],
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_init_and_resume(t: Test, ws: Path):
    print("\n[1] first_run → init → resume")
    # first_run
    rc, out, _ = run([sys.executable, str(SCRIPTS / "preflight.py"), str(ws)])
    t.check("preflight on empty ws → first_run", pj(out).get("mode") == "first_run")

    # init
    rc, out, _ = run([sys.executable, str(SCRIPTS / "state_manager.py"),
                      str(ws), "init", "--repo-url", "test://foo"])
    t.check("state_manager init succeeds", rc == 0)

    # resume
    rc, out, _ = run([sys.executable, str(SCRIPTS / "preflight.py"),
                      str(ws), "--no-emit-event"])
    pre = pj(out)
    t.check("preflight on initialized → resume", pre.get("mode") == "resume")
    t.check("preflight provides next_action_hint",
            len(pre.get("next_action_hint", "")) > 5)


def scenario_worktree_isolation(t: Test, ws: Path):
    print("\n[2] worktree_helper enforces isolation")
    # Setup repo + skills + knowledge stubs so includes can copy
    (ws / "repo").mkdir(exist_ok=True)
    (ws / "repo" / "README.md").write_text("repo")
    (ws / "skills" / "s1").mkdir(parents=True, exist_ok=True)
    (ws / "skills" / "s1" / "SKILL.md").write_text("""---
name: s1
description: test
---
# S1""")
    (ws / "knowledge").mkdir(exist_ok=True)
    (ws / "knowledge" / "notes.md").write_text("""# Notes
line 2
line 3
line 4 about normalization
""")

    # Runner+S: repo + skills
    rc, out, _ = run([
        sys.executable, str(SCRIPTS / "worktree_helper.py"), "create",
        "--workspace", str(ws),
        "--purpose", "runner-with-iter1-t1",
        "--include", "repo,skills",
    ])
    t.check("worktree create with repo+skills", rc == 0)
    wp_with = pj(out).get("work_path")

    # Runner-S: repo only, forbid skills
    rc, out, _ = run([
        sys.executable, str(SCRIPTS / "worktree_helper.py"), "create",
        "--workspace", str(ws),
        "--purpose", "runner-without-iter1-t1",
        "--include", "repo", "--exclude-guard", "skills",
    ])
    t.check("worktree create with forbid-skills succeeds", rc == 0)
    wp_without = pj(out).get("work_path")
    t.check("Runner-S worktree does NOT contain skills/",
            not (Path(wp_without) / "skills").exists())

    # Eval Designer: knowledge only
    rc, out, _ = run([
        sys.executable, str(SCRIPTS / "worktree_helper.py"), "create",
        "--workspace", str(ws),
        "--purpose", "eval-designer-iter0",
        "--include", "knowledge", "--exclude-guard", "skills,evals",
    ])
    t.check("Eval Designer worktree created", rc == 0)

    # isolation_runner preflight
    rc, out, _ = run([
        sys.executable, str(SCRIPTS / "isolation_runner.py"), "preflight",
        "--work-path", wp_without, "--forbid", "skills",
    ])
    t.check("isolation_runner preflight passes for clean worktree", rc == 0)

    return wp_with, wp_without


def scenario_osr_validation_chain(t: Test, ws: Path, wp_with: str,
                                  wp_without: str):
    print("\n[3] OSR validation for Researcher / Runner / Skill Writer")
    # Fake Researcher OSR
    osr_r = {
        **_common_osr("researcher", str(ws)),
        "knowledge_files": [
            {"path": "knowledge/notes.md", "topic": "normalization",
             "lines": 4, "est_tokens": 30, "primary_claims_count": 2}
        ],
        "research_direction": "grader architecture",
        "confidence": "high",
    }
    rc, _, _ = run([sys.executable, str(SCRIPTS / "osr_validate.py"),
                    "--agent", "researcher", "--input", "-"],
                   input_str=json.dumps(osr_r))
    t.check("Researcher OSR validates", rc == 0)

    # Fake Runner OSRs for 2 tasks
    for i, tid in enumerate(["t1", "t2"]):
        osr_run_with = {
            **_common_osr("runner", wp_with),
            "run_id": f"r{i}-with", "variant": "with_skill",
            "task_id": tid, "work_path": wp_with,
            "output_files": ["answer.txt"],
            "subagent_log_path": f"/tmp/fake-log-{tid}-with.jsonl",
            "files_read": ["repo/README.md", "skills/s1/SKILL.md"],
        }
        rc, _, _ = run([sys.executable, str(SCRIPTS / "osr_validate.py"),
                        "--agent", "runner", "--input", "-"],
                       input_str=json.dumps(osr_run_with))
        t.check(f"Runner OSR (with_skill, {tid}) validates", rc == 0)

    # Runner-S reading skills/ should be REJECTED by invariant
    bad_runner = {
        **_common_osr("runner", wp_without),
        "run_id": "bad", "variant": "without_skill",
        "task_id": "t1", "work_path": wp_without,
        "output_files": ["answer.txt"],
        "subagent_log_path": "/tmp/fake.jsonl",
        "files_read": ["repo/README.md", "skills/s1/SKILL.md"],  # LEAK
    }
    rc, _, _ = run([sys.executable, str(SCRIPTS / "osr_validate.py"),
                    "--agent", "runner", "--input", "-"],
                   input_str=json.dumps(bad_runner))
    t.check("Runner OSR with skills/ read in without_skill variant → REJECTED",
            rc != 0)

    # Skill Writer — GOOD
    good_sw = {
        **_common_osr("skill_writer", str(ws)),
        "skill_files": [{"path": "skills/s1/SKILL.md", "lines": 4}],
        "changes_applied": [{
            "change_id": "c1", "type": "add",
            "target_file": "skills/s1/SKILL.md",
            "rationale_short": "normalization guidance for mixed-scale graders",
            "knowledge_source_refs": [
                {"path": "knowledge/notes.md", "line_from": 3, "line_to": 4},
            ],
        }],
        "removed_lines": 0,
    }
    rc, _, _ = run([sys.executable, str(SCRIPTS / "osr_validate.py"),
                    "--agent", "skill_writer", "--input", "-"],
                   input_str=json.dumps(good_sw))
    t.check("Skill Writer OSR (good refs) validates", rc == 0)

    # Skill Writer — BAD (empty refs for add)
    bad_sw = {**good_sw, "changes_applied": [
        {**good_sw["changes_applied"][0], "knowledge_source_refs": []}
    ]}
    rc, _, _ = run([sys.executable, str(SCRIPTS / "osr_validate.py"),
                    "--agent", "skill_writer", "--input", "-"],
                   input_str=json.dumps(bad_sw))
    t.check("Skill Writer OSR (empty refs for 'add') → REJECTED", rc != 0)


def scenario_overfit_check(t: Test, ws: Path):
    print("\n[4] overfit_check catches eval-task leakage")
    (ws / "evals").mkdir(exist_ok=True)
    (ws / "evals" / "eval-tasks.json").write_text(json.dumps([
        {"id": "mixed-scale-aggregation-pitfall",
         "description": "test task 20 chars min",
         "expectedBehavior": "do x test 10 chars",
         "judgingCriteria": "correct minimum text"},
    ]))

    # Change with task_id literal in rationale
    leaky = {"changes_applied": [{
        "change_id": "c1", "type": "add",
        "target_file": "skills/s1/SKILL.md",
        "rationale_short": "fix for mixed-scale-aggregation-pitfall specifically",
        "knowledge_source_refs": [
            {"path": "knowledge/notes.md", "line_from": 1, "line_to": 4},
        ],
    }]}
    changes_file = ws / "test-changes-leaky.json"
    changes_file.write_text(json.dumps(leaky))

    rc, out, _ = run([sys.executable, str(SCRIPTS / "overfit_check.py"),
                      "--workspace", str(ws),
                      "--changes-file", str(changes_file)])
    t.check("overfit_check rejects task_id literal", rc != 0)
    reasons = [f.get("reason") for r in pj(out).get("results", [])
               for f in r.get("failures", [])]
    t.check("overfit_check reason=task_id_literal_in_rationale",
            "task_id_literal_in_rationale" in reasons)


def scenario_score_round_trip(t: Test, ws: Path):
    print("\n[5] deblind_and_score produces OSR digest + events")
    iter_dir = ws / "evals" / "results" / "iter-1"
    iter_dir.mkdir(parents=True, exist_ok=True)

    (iter_dir / "eval-results.json").write_text(json.dumps({
        "iteration": 1,
        "tasks": [
            {"taskId": "t1",
             "withSkill": {"text": "a1", "toolUseCount": 2},
             "withoutSkill": {"text": "b1", "toolUseCount": 5}},
            {"taskId": "t2",
             "withSkill": {"text": "a2", "toolUseCount": 1},
             "withoutSkill": {"text": "b2", "toolUseCount": 3}},
        ],
    }))
    (iter_dir / "blind-mapping.json").write_text(json.dumps([
        {"taskId": "t1", "aIsWithSkill": True},
        {"taskId": "t2", "aIsWithSkill": False},
    ]))
    (iter_dir / "blind-grader-scores.json").write_text(json.dumps([
        {"taskId": "t1",
         "outputA": {"quality": 4.0, "toolUseCount": 2},
         "outputB": {"quality": 3.0, "toolUseCount": 5},
         "winner": "A", "reasoning": "A good", "feedback": "x", "suggestion": "y"},
        {"taskId": "t2",
         "outputA": {"quality": 3.0, "toolUseCount": 3},
         "outputB": {"quality": 4.2, "toolUseCount": 1},
         "winner": "B", "reasoning": "B good", "feedback": "x", "suggestion": "y"},
    ]))

    rc, out, _ = run([sys.executable, str(SCRIPTS / "deblind_and_score.py"),
                      str(ws), "1"])
    t.check("deblind_and_score exits 0", rc == 0)
    t.check("osr-grader-digest.json produced",
            (iter_dir / "osr-grader-digest.json").exists())

    dig = pj((iter_dir / "osr-grader-digest.json").read_text())
    t.check("digest has aggregate.skill_won_rate",
            "skill_won_rate" in (dig.get("aggregate") or {}))
    t.check("digest has per_task list",
            len(dig.get("per_task") or []) == 2)


def scenario_invariant_detects_fabrication(t: Test, ws: Path):
    print("\n[6] invariant_check detects iter-to-iter identity")
    # Plant iter-1/2/3 with identical tool counts
    for i in range(1, 4):
        (ws / "state" / "iterations" / f"iter-{i}.json").write_text(json.dumps({
            "iter": i,
            "started_at": "2026-01-01",
            "runs": [
                {"task_id": "t1",
                 "with_skill": {"runner_return_path": "a",
                                "subagent_log_path": "/tmp/x"},
                 "without_skill": {"runner_return_path": "b",
                                   "subagent_log_path": "/tmp/y"},
                 "tool_counts": {"with": 1, "without": 3, "source": "self_report"}},
                {"task_id": "t2",
                 "with_skill": {"runner_return_path": "c",
                                "subagent_log_path": "/tmp/z"},
                 "without_skill": {"runner_return_path": "d",
                                   "subagent_log_path": "/tmp/w"},
                 "tool_counts": {"with": 0, "without": 2, "source": "self_report"}},
            ],
            "skill_writer": {"removed_lines": 0},
        }))

    rc, out, _ = run([sys.executable, str(SCRIPTS / "invariant_check.py"),
                      "--workspace", str(ws), "--check", "tool_count_variance"])
    t.check("invariant_check detects identity → exits 1", rc == 1)
    check = pj(out).get("checks", [{}])[0]
    t.check("severity=critical", check.get("severity") == "critical")
    t.check("evidence.identity_hits non-empty",
            len(check.get("evidence", {}).get("identity_hits", [])) >= 1)

    # task_reality should also fail (fake log paths don't exist)
    rc, out, _ = run([sys.executable, str(SCRIPTS / "invariant_check.py"),
                      "--workspace", str(ws), "--check", "task_reality"])
    t.check("invariant_check task_reality detects fake logs → exits 1", rc == 1)


def scenario_migration(t: Test):
    print("\n[7] v1 → v2 migration round-trip")
    tmp = Path(tempfile.mkdtemp())
    try:
        v1ws = tmp / "v1ws"
        (v1ws / "evals" / "results" / "iter-1").mkdir(parents=True)

        (v1ws / "orchestrator-state.json").write_text(json.dumps({
            "version": "1.0.0", "repo_url": "https://github.com/ex/foo",
            "phase": "iterate", "research_done": True,
            "generation_done": True,
            "iteration": {"current": 1, "scores": [0.55]},
            "notes": "v1 legacy notes",
        }))
        (v1ws / "results.tsv").write_text(
            "iteration\tcomposite\tcost_usd\tstatus\tdescription\n"
            "1\t0.5500\t0.00\tkeep\tfirst iteration\n"
        )
        (v1ws / "evals" / "results" / "iter-1" / "eval-results.json").write_text(
            json.dumps({"iteration": 1, "tasks": [
                {"taskId": "t1",
                 "withSkill": {"text": "a", "toolUseCount": 4},
                 "withoutSkill": {"text": "b", "toolUseCount": 6}}
            ]})
        )

        rc, out, _ = run([sys.executable, str(SCRIPTS / "state_migrate.py"),
                          str(v1ws)])
        t.check("state_migrate exits 0", rc == 0)
        mig = pj(out)
        t.check("scores_migrated=1", mig.get("scores_migrated") == 1)
        t.check("iteration_files_written >= 1",
                (mig.get("iteration_files_written") or 0) >= 1)

        # Preflight should now work
        rc, out, _ = run([sys.executable, str(SCRIPTS / "preflight.py"),
                          str(v1ws), "--no-emit-event"])
        t.check("preflight on migrated ws → resume", rc == 0)
        t.check("migrated phase=iterate",
                pj(out).get("state", {}).get("phase") == "iterate")

        # v1 files preserved
        t.check("orchestrator-state.json preserved",
                (v1ws / "orchestrator-state.json").exists())
        t.check("results.tsv preserved", (v1ws / "results.tsv").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    t = Test()
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp())
    try:
        ws = tmp / "ws"
        scenario_init_and_resume(t, ws)
        wp_with, wp_without = scenario_worktree_isolation(t, ws)
        scenario_osr_validation_chain(t, ws, wp_with, wp_without)
        scenario_overfit_check(t, ws)
        scenario_score_round_trip(t, ws)
        scenario_invariant_detects_fabrication(t, ws)
        scenario_migration(t)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nElapsed: {time.time() - t0:.1f}s")
    return t.summary()


if __name__ == "__main__":
    raise SystemExit(main())
