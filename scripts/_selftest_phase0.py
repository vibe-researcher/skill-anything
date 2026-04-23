#!/usr/bin/env python3
"""Self-tests for Phase 0 infrastructure.

Exercises state_manager.py + osr_validate.py across the full happy path plus
several failure modes. Run from the repo root:

    python scripts/_selftest_phase0.py

Exits 0 on success, 1 if any assertion fails. Prints a compact pass/fail
summary plus detailed output for failures.

This is intentionally NOT pytest — matches the rest of the codebase's
stdlib-only style.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_MGR = REPO_ROOT / "scripts" / "state_manager.py"
OSR_VALIDATE = REPO_ROOT / "scripts" / "osr_validate.py"
PREFLIGHT = REPO_ROOT / "scripts" / "preflight.py"


class TestResult:
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
            self.failures.append((label, detail))
            print(f"  FAIL  {label}")
            if detail:
                print(f"         {detail}")

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{'=' * 60}")
        print(f"Phase 0 self-test: {self.passed}/{total} passed, {self.failed} failed")
        if self.failures:
            print("\nFailures:")
            for label, detail in self.failures:
                print(f"  - {label}: {detail}")
            return 1
        return 0


def run(cmd: list, input_str: str | None = None) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd, capture_output=True, text=True,
        input=input_str, timeout=30,
    )
    return p.returncode, p.stdout, p.stderr


def parse_json(s: str) -> dict:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Test 1 — state_manager happy path + atomic semantics
# ---------------------------------------------------------------------------


def test_state_manager_happy_path(r: TestResult):
    print("\n[1] state_manager happy path")
    tmp = Path(tempfile.mkdtemp())
    try:
        ws = tmp / "ws"

        rc, out, err = run(["python3", str(STATE_MGR), str(ws), "init",
                            "--repo-url", "test://x"])
        r.check("init exits 0", rc == 0, err)
        init_result = parse_json(out)
        r.check("init returns ok=true", init_result.get("ok") is True)
        r.check("init returns session_id",
                "session_id" in init_result and len(init_result["session_id"]) > 10)
        r.check("state.json created", (ws / "state.json").exists())
        r.check("events.jsonl created", (ws / "state" / "events.jsonl").exists())
        r.check("iterations/ created", (ws / "state" / "iterations").is_dir())
        r.check("surprises/ created", (ws / "state" / "surprises").is_dir())
        r.check("notes/ created", (ws / "notes").is_dir())

        # phase transition
        rc, out, _ = run(["python3", str(STATE_MGR), str(ws),
                          "phase-transition", "--to", "research"])
        r.check("phase-transition exits 0", rc == 0)

        rc, out, _ = run(["python3", str(STATE_MGR), str(ws),
                          "get", "--key", "phase"])
        r.check("phase is 'research' after transition",
                parse_json(out).get("value") == "research")

        # set arbitrary field
        rc, _, _ = run(["python3", str(STATE_MGR), str(ws), "set",
                        "--key", "research_done", "--value", "true"])
        r.check("set research_done=true exits 0", rc == 0)
        rc, out, _ = run(["python3", str(STATE_MGR), str(ws),
                          "get", "--key", "research_done"])
        r.check("research_done persisted",
                parse_json(out).get("value") is True)

        # append-event
        rc, out, _ = run(["python3", str(STATE_MGR), str(ws),
                          "append-event", "--event-type", "task_spawned",
                          "--agent", "researcher", "--summary", "spawn test"])
        r.check("append-event exits 0", rc == 0)
        ev_id = parse_json(out).get("event_id")
        r.check("append-event returns event_id", bool(ev_id))

        # append-score
        rc, _, _ = run(["python3", str(STATE_MGR), str(ws),
                        "append-score", "--iter", "1", "--composite", "0.72",
                        "--delta", "0.15", "--cost", "0.5"])
        r.check("append-score exits 0", rc == 0)
        rc, out, _ = run(["python3", str(STATE_MGR), str(ws),
                          "get", "--key", "scores_history"])
        sh = parse_json(out).get("value", [])
        r.check("scores_history has 1 entry", len(sh) == 1)
        r.check("scores_history entry has composite=0.72",
                sh and sh[0]["composite"] == 0.72)

        # write-iter / read-iter
        iter_data = {"iter": 1, "started_at": "2026-04-22T11:00:00Z"}
        rc, _, _ = run(["python3", str(STATE_MGR), str(ws),
                        "write-iter", "--iter", "1",
                        "--data", json.dumps(iter_data)])
        r.check("write-iter exits 0", rc == 0)
        rc, out, _ = run(["python3", str(STATE_MGR), str(ws),
                          "read-iter", "--iter", "1"])
        r.check("read-iter round-trips",
                parse_json(out).get("data", {}).get("iter") == 1)

        # snapshot
        rc, _, _ = run(["python3", str(STATE_MGR), str(ws),
                        "snapshot", "--reason", "end of iter 1"])
        r.check("snapshot exits 0", rc == 0)

        # events.jsonl should have multiple entries
        lines = (ws / "state" / "events.jsonl").read_text().strip().splitlines()
        r.check(f"events.jsonl has >=5 entries (got {len(lines)})",
                len(lines) >= 5)

        # Every line valid JSON
        all_valid = all(parse_json(l) for l in lines)
        r.check("all events are valid JSON", all_valid)

        # final state.json still valid
        final = parse_json((ws / "state.json").read_text())
        r.check("final state.json parses",
                final.get("schema_version") == "2.0.0")
        r.check("final state.json current_iteration is 1",
                final.get("current_iteration") == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 2 — atomic write resilience: concurrent updates
# ---------------------------------------------------------------------------


def test_concurrent_updates(r: TestResult):
    print("\n[2] state_manager concurrent updates stay consistent")
    tmp = Path(tempfile.mkdtemp())
    try:
        ws = tmp / "ws"
        run(["python3", str(STATE_MGR), str(ws), "init",
             "--repo-url", "test://x"])

        # Run 20 append-event calls in parallel threads
        N = 20
        errors: list = []

        def worker(i: int):
            rc, _, err = run([
                "python3", str(STATE_MGR), str(ws),
                "append-event", "--event-type", "task_spawned",
                "--summary", f"event-{i}",
            ])
            if rc != 0:
                errors.append((i, err))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        r.check(f"all {N} concurrent appends succeeded",
                len(errors) == 0,
                f"errors: {errors[:3]}")

        lines = (ws / "state" / "events.jsonl").read_text().strip().splitlines()
        # Should have 1 initial event + 1 from init phase + N appended
        r.check(f"events.jsonl has >={N} entries (got {len(lines)})",
                len(lines) >= N)

        # All lines valid JSON and distinct event_ids
        parsed = [parse_json(l) for l in lines]
        r.check("all lines valid JSON after concurrent writes",
                all(parsed))
        ids = [p.get("event_id") for p in parsed]
        r.check("all event_ids unique", len(set(ids)) == len(ids))

        # state.json itself still valid
        st = parse_json((ws / "state.json").read_text())
        r.check("state.json still valid after concurrent writes",
                st.get("schema_version") == "2.0.0")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 3 — osr_validate: valid samples pass
# ---------------------------------------------------------------------------


def _common_fields(agent_type: str) -> dict:
    return {
        "status": "success",
        "agent_type": agent_type,
        "agent_env": {"cwd": "/tmp", "wall_time_s": 1.0},
        "surprises": [],
        "anomalies": [],
        "meta_observations": [],
    }


def _validate(agent: str, payload: dict) -> tuple[int, str]:
    rc, out, _ = run(
        ["python3", str(OSR_VALIDATE), "--agent", agent, "--input", "-"],
        input_str=json.dumps(payload),
    )
    return rc, out


def test_osr_validate_valid_samples(r: TestResult):
    print("\n[3] osr_validate accepts valid samples of all 5 agents")

    # Researcher
    rc, out = _validate("researcher", {
        **_common_fields("researcher"),
        "knowledge_files": [{"path": "knowledge/a.md", "topic": "t", "lines": 10}],
        "research_direction": "test direction",
        "confidence": "high",
    })
    r.check("researcher valid", rc == 0, out)

    # Skill Writer (with proper knowledge refs)
    rc, out = _validate("skill_writer", {
        **_common_fields("skill_writer"),
        "skill_files": [{"path": "skills/x/SKILL.md", "lines": 100}],
        "changes_applied": [{
            "change_id": "c1",
            "type": "add",
            "target_file": "skills/x/SKILL.md",
            "rationale_short": "add structured_model config guidance",
            "knowledge_source_refs": [
                {"path": "knowledge/grader-architecture.md",
                 "line_from": 120, "line_to": 145},
            ],
        }],
        "removed_lines": 0,
    })
    r.check("skill_writer valid (with refs)", rc == 0, out)

    # Eval Designer
    rc, out = _validate("eval_designer", {
        **_common_fields("eval_designer"),
        "eval_tasks_file": "evals/eval-tasks.json",
        "task_ids": ["t1", "t2"],
        "skill_term_leakage_check": {"passed": True, "terms_found": []},
        "isolation_env_path": "/tmp/ed-isolated-xyz",
    })
    r.check("eval_designer valid", rc == 0, out)

    # Runner
    rc, out = _validate("runner", {
        **_common_fields("runner"),
        "run_id": "r1",
        "variant": "with_skill",
        "task_id": "t1",
        "work_path": "/tmp/r1",
        "output_files": ["out.txt"],
        "subagent_log_path": "/tmp/logs/r1.jsonl",
    })
    r.check("runner valid (with_skill)", rc == 0, out)

    # Grader
    rc, out = _validate("grader", {
        **_common_fields("grader"),
        "scores_file": "evals/results/iter-1/blind-grader-scores.json",
        "per_task": [{
            "task_id": "t1", "winner": "A",
            "quality_a": 4.0, "quality_b": 3.0,
            "tool_count_a_from_log": 1, "tool_count_b_from_log": 3,
        }],
        "aggregate": {
            "winner_dist": {"A": 1, "B": 0, "TIE": 0},
            "quality_a_range": [4.0, 4.0],
            "quality_b_range": [3.0, 3.0],
        },
        "blind_discipline_check": {
            "referenced_skill_files": False,
            "inferred_identity": False,
        },
    })
    r.check("grader valid", rc == 0, out)


# ---------------------------------------------------------------------------
# Test 4 — osr_validate: invariants reject bad payloads
# ---------------------------------------------------------------------------


def test_osr_validate_invariants(r: TestResult):
    print("\n[4] osr_validate enforces invariants")

    # schema_insufficient without requested_schema_extension
    rc, out = _validate("researcher", {
        **_common_fields("researcher"),
        "status": "schema_insufficient",
        "knowledge_files": [{"path": "k.md", "topic": "t", "lines": 1}],
        "research_direction": "x",
        "confidence": "low",
    })
    r.check("schema_insufficient without extension → reject", rc == 1)

    # Skill Writer: empty knowledge_source_refs for type=add
    rc, out = _validate("skill_writer", {
        **_common_fields("skill_writer"),
        "skill_files": [{"path": "skills/x/SKILL.md", "lines": 1}],
        "changes_applied": [{
            "change_id": "c1", "type": "add",
            "target_file": "skills/x/SKILL.md",
            "rationale_short": "r",
            "knowledge_source_refs": [],
        }],
        "removed_lines": 0,
    })
    r.check("skill_writer empty refs for 'add' → reject", rc == 1)

    # Skill Writer: empty refs for type=delete is OK
    rc, out = _validate("skill_writer", {
        **_common_fields("skill_writer"),
        "skill_files": [{"path": "skills/x/SKILL.md", "lines": 1}],
        "changes_applied": [{
            "change_id": "c1", "type": "delete",
            "target_file": "skills/x/SKILL.md",
            "rationale_short": "remove stale section",
            "knowledge_source_refs": [],
        }],
        "removed_lines": 20,
    })
    r.check("skill_writer empty refs for 'delete' → accept", rc == 0, out)

    # Runner: without_skill reads a skills/ path
    rc, out = _validate("runner", {
        **_common_fields("runner"),
        "run_id": "r1",
        "variant": "without_skill",
        "task_id": "t1",
        "work_path": "/tmp/r1",
        "output_files": ["out.txt"],
        "subagent_log_path": "/tmp/logs/r1.jsonl",
        "files_read": ["repo/a.py", "skills/x/SKILL.md"],
    })
    r.check("without_skill Runner reading skills/ → reject", rc == 1)

    # Missing required field
    rc, out = _validate("researcher", {
        **_common_fields("researcher"),
        "knowledge_files": [{"path": "k.md", "topic": "t", "lines": 1}],
        "confidence": "low",
        # research_direction missing
    })
    r.check("missing research_direction → reject", rc == 1)

    # Enum violation
    rc, out = _validate("researcher", {
        **_common_fields("researcher"),
        "knowledge_files": [{"path": "k.md", "topic": "t", "lines": 1}],
        "research_direction": "x",
        "confidence": "extreme",  # not in enum
    })
    r.check("bad confidence enum → reject", rc == 1)

    # Open-world: unknown top-level field accepted
    rc, out = _validate("researcher", {
        **_common_fields("researcher"),
        "knowledge_files": [{"path": "k.md", "topic": "t", "lines": 1}],
        "research_direction": "x",
        "confidence": "low",
        "experimental_new_field": "preserved verbatim",
    })
    r.check("unknown top-level field (extras) → accept", rc == 0, out)


# ---------------------------------------------------------------------------
# Test 5 — preflight integration
# ---------------------------------------------------------------------------


def test_preflight(r: TestResult):
    print("\n[5] preflight detects first_run / resume / corruption")
    tmp = Path(tempfile.mkdtemp())
    try:
        ws = tmp / "ws"

        # first_run
        rc, out, _ = run(["python3", str(PREFLIGHT), str(ws)])
        r.check("preflight on nonexistent ws exits 0", rc == 0)
        r.check("preflight reports first_run",
                parse_json(out).get("mode") == "first_run")

        # init and resume
        run(["python3", str(STATE_MGR), str(ws), "init",
             "--repo-url", "test://x"])
        rc, out, _ = run(["python3", str(PREFLIGHT), str(ws), "--no-emit-event"])
        r.check("preflight after init exits 0", rc == 0)
        pre = parse_json(out)
        r.check("preflight reports resume mode", pre.get("mode") == "resume")
        r.check("preflight provides state", "state" in pre)
        r.check("preflight provides last_event", "last_event" in pre)
        r.check("preflight provides next_action_hint",
                isinstance(pre.get("next_action_hint"), str)
                and len(pre["next_action_hint"]) > 5)

        # corruption detection
        (ws / "state.json").write_text("{ this is not json ")
        rc, out, _ = run(["python3", str(PREFLIGHT), str(ws), "--no-emit-event"])
        r.check("preflight detects corrupt state.json (exit=1)", rc == 1)
        r.check("preflight reports fresh_init_needed on corruption",
                parse_json(out).get("mode") == "fresh_init_needed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    r = TestResult()
    t0 = time.time()
    test_state_manager_happy_path(r)
    test_concurrent_updates(r)
    test_osr_validate_valid_samples(r)
    test_osr_validate_invariants(r)
    test_preflight(r)
    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.1f}s")
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())
