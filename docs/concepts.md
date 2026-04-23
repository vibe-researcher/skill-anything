# skill-anything Concepts

*An English navigation hub for the v2 harness. For the full Chinese architecture writeup, see [`harness-design.md`](./harness-design.md).*

---

## The core idea

An **Agent-Skill Compiler** sits between raw repo documentation and the agent. It:

1. **Reads** the target repo like a researcher (Researcher agents, in parallel).
2. **Writes** a small number of `SKILL.md` bundles that encode the tacit domain model.
3. **Tests** its own output with blind A/B evaluation against a no-Skill baseline.
4. **Iterates** until the composite score converges or a guardrail halts the loop.

The difference from a prompt template: the Skill is **graded**, and the grading has receipts.

---

## Architectural primitives

### Claude as Orchestrator

The top-level loop is run by a single Claude Code instance reading [`SKILL.md`](../SKILL.md) at the repo root. No SDK, no Python pipeline. Complexity lives in natural language. This makes the system portable to every Claude-compatible platform without code changes.

The Orchestrator **does not** compute quality scores itself. It spawns isolated sub-agents, consumes their structured returns, and decides what to do next.

### Physical worktree isolation

v1 relied on the Orchestrator *not* looking at files it wasn't supposed to look at. It did anyway. v2 runs each sub-agent (Runner, Grader, Eval Designer) inside `isolation: "worktree"` — a scoped git checkout that the tool runtime enforces at the filesystem layer.

Consequences:
- The Eval Designer has no filesystem path to `skills/` — it cannot "accidentally" see the Skill it is designing tasks for.
- The Grader has no path to `blind-mapping.json` — it physically cannot de-blind.
- The Orchestrator cannot construct a Grader's output without fabricating filesystem state it never saw, which is much harder than fabricating a JSON blob inline.

See the [full rationale](./why-v2.md) for why protocol isolation isn't enough.

### OSR — Output Schema Return

Every sub-agent returns a JSON payload conforming to a published schema (`schemas/osr-*.schema.json`). The Orchestrator consumes fields mechanically. This means:

- A malformed return fails loudly instead of silently drifting.
- The Orchestrator never has to "interpret what the agent meant" — the protocol is typed.
- Sub-agent transcripts remain small (they return JSON, not paragraphs), reducing context pressure.

### Markov-style state

Everything the loop needs to resume is on disk:

- `workspace/orchestrator-state.json` — phase, iteration count, pending channels, guardrail flags.
- `workspace/evals/results/iter-N/` — per-iteration artifacts.
- `workspace/skills/` — the current Skill draft.

A mid-flight context reset is architecturally indistinguishable from a normal tick. The Orchestrator reads state and the last event; it does not rely on conversation memory.

### Grader ensemble (K=3)

A single Grader is a single point of bias. K independent Graders vote (majority on `skill_won`, median on quality score). Disagreement becomes a **signal** — when K=3 graders disagree, the loop flags for investigation instead of averaging it away.

The OpenJudge run shipped with K=1 as a cost trade-off; the recommended default is K=3 and re-running at K=3 is on the roadmap.

### Nine guardrails

Hard invariants that halt the loop. Partial list:

- **G1 skill-won-rate > 90% for 2 rounds** → overfit investigation.
- **G2 tool-count variance below threshold** → suspected Runner fabrication.
- **G3 grader-rationale language mismatch** → suspected leak of withSkill identity.
- **G4 composite regresses ≥ ε with no quality-regression explanation** → rollback candidate.

Full list and thresholds in [`references/eval-loop.md`](../references/eval-loop.md).

---

## The iteration loop

```
Run        →  Runner × 2/task (with-Skill, baseline) in isolated worktrees
Blind      →  blind_eval.py randomizes A/B labels
Grade      →  Grader × K in isolated worktrees; each returns OSR JSON
Score      →  deblind_and_score.py: composite = 0.6·quality + 0.4·trajectory
Improve    →  Skill Writer reads grader suggestions, updates SKILL.md
Converge?  →  Δ < 0.03 for 2 rounds → keep; regression → rollback; guardrail → halt
```

The per-step protocol is documented in [`references/eval-loop.md`](../references/eval-loop.md).

---

## Further reading

- [`harness-design.md`](./harness-design.md) — the full Chinese architecture writeup (444 lines). Theory, control-loop framing, and design-decision tables.
- [`lessons-learned-openjudge-2026-04-23.md`](./lessons-learned-openjudge-2026-04-23.md) — root-cause review after running the v2 harness against OpenJudge. Includes the iter-3 regression analysis.
- [`why-v2.md`](./why-v2.md) — the v1 failure postmortem that forced the architectural rebuild.
- [`../references/eval-loop.md`](../references/eval-loop.md) — detailed iteration protocol (run/blind/grade/score/improve).
- [`../agents/`](../agents/) — role-specific guides (Researcher, Skill Writer, Eval Designer, Grader, Investigator).
- [`../schemas/`](../schemas/) — OSR schemas for every sub-agent return type.
- [`../critique-report.json`](../critique-report.json) — the 16-problem critique of v1 that informed v2's design.
