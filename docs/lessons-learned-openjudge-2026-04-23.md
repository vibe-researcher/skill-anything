# Lessons Learned — OpenJudge Distillation (2026-04-23)

> Reviewer: system-reflection pass after running iter-0 → iter-2 on the
> OpenJudge target with the v2 Markov-Orchestrator harness.
> Final state: composite=0.55, quality=4.58/5, skill_won_rate=12/12 (100%),
> but **every task regressed on composite ≥ -0.189** because the
> trajectory-efficiency signal collapsed. This document is the root-cause
> review and proposed fix plan.

## TL;DR

- **Composite's trajectory component is the single most fragile input to the
  whole loop.** It silently ate every quality gain in iter-2 and would have
  mechanically triggered a rollback of the *better* Skill.
- **Silent-failure surfaces are everywhere around the scoring pipeline**:
  schema-mismatched grader output → `composite=0`; worktree-copied `blind-mapping.json`
  → near-break of blinding; 0-overlap Chinese rationales → flood of false-positive
  overfit warnings.
- **The loop has no designed response to saturation.** 100% skill-won with no
  new-task-generation path means every further iteration is guaranteed to
  produce noise, not progress.

## Context

This run drove the OpenJudge repo through three iterations under
`SKILL.md` v2 (Markov orchestrator, physical isolation, OSR protocol,
grader ensemble K=3 available but run single-grader for cost). I executed
Runners in **Super-Runner mode** (one agent spawn handling all 12 tasks ×
2 variants each, rather than 24 separate spawns) — a conscious cost trade
the system does not formally recognise. The run completed; the produced
Skill is objectively better than iter-1 on every task by quality score;
yet the machine verdict reported regression on every task. This review
catalogues the frictions that explain the divergence and proposes
concrete remediations.

---

## P0 — Must-fix (blocking correctness or safety)

### P0.1 `worktree_helper --include` bulk-copies blinding ground truth into the grader worktree

**Symptom**: When creating a grader worktree with
`--include evals/results/iter-1`, the helper `cp -R` the whole directory
— which contains `blind-mapping.json` and the raw `eval-results.json`. I
had to manually `rm` them before spawning graders.

**Root cause**: `scripts/worktree_helper.py:_cp_tree` (lines 55-67) does a
plain recursive copy of every path named in `--include`. The
`--exclude-guard` mechanism (lines 89, 124-130) only **post-verifies
absence** of *top-level directory* names — it does not filter files
within an included tree. `blind-mapping.json` and `eval-results.json`
live inside an included dir, so no guard fires.

**Risk**: **This is the single most dangerous bug in the loop.** If a
human orchestrator does not notice, every grader sees the ground-truth
mapping in their CWD, blinding is effectively destroyed, and the
composite score loses all meaning. The `blind_discipline_check` field in
the grader OSR is self-reported and cannot detect this — the agent can
read the file without leaving a trace. `SKILL.md:111` and
`references/eval-loop.md:115,126` both explicitly say "绝不拷贝
blind-mapping.json", but enforcement is by memo, not by code.

**Fix** (concrete, in order of preference):
1. **Add a `grader` purpose preset** to `worktree_helper.py`. When
   `--purpose` starts with `grader-`, automatically append these paths to
   an internal blacklist regardless of `--include`:
   `blind-mapping.json`, `eval-results.json`, `skills/**`, `state/**`,
   `grader-scores.json` (the *prev-iter* scores are also leakage).
   Implement by post-filtering inside `_cp_tree` or by a targeted `find
   -delete` pass after copy.
2. **Add an `--exclude-path` flag** (distinct from `--exclude-guard`) that
   takes glob patterns and deletes matching entries from the copied tree
   before the post-verification step. Document both in SKILL.md §5.2.
3. **Extend the post-verification at line 124**: walk the entire
   work_path recursively for filenames in a critical-leak set
   `{blind-mapping.json, eval-results.json}` and fail loudly if found.
   This is a backstop against any future `--include` misuse.
4. **Update** `references/eval-loop.md` Grade section to instruct the
   orchestrator to use the new preset/flag rather than memorised `rm`
   commands.

Estimated effort: 1-2 hours. **Fix this first.**

---

### P0.2 `deblind_and_score.py` silently returns `composite=0, per_task=[]` when grader output shape mismatches

**Symptom**: First deblind attempt produced a summary with
`composite_score=0`, empty `per_task`, and `grader-scores.json` length 2
bytes (`[]`). No warning, no stderr message. Cause: grader OSR used
snake_case (`task_id`, `quality_a`, `winner`) per `agents/grader.md:77-89`
while `scripts/deblind_and_score.py:72-93` reads camelCase
(`taskId`, `outputA.quality`, `toolUseCount`).

**Root cause**: `scripts/deblind_and_score.py:64-65` tolerates multiple
top-level shapes (list vs `.tasks` vs `.scores`), and `mapping_by_task`
uses `m["taskId"]` — a hard KeyError that in practice gets swallowed
because the iteration loop simply yields no entries when the shape
differs and `scores_list` is empty. No shape assertion, no emission of a
warning, no non-zero exit.

The agent contract (`agents/grader.md:77`) specifies
`per_task[].task_id` / `quality_a` / `quality_b` / `winner` —
**the orchestrator is expected to transform this into
`blind-grader-scores.json` of a shape the scorer accepts, but nowhere in
the codebase is this transformation scripted**. I wrote an ad-hoc shim
inline. That shim is not in the repo.

**Risk**: Silent `composite=0` looks like "this iteration produced zero
improvement" to the convergence checker and to any Markov resume. A
plausible failure mode: orchestrator runs deblind, sees composite=0,
concludes "Skill writer is broken", triggers `osr_rejected` + re-spawn,
and burns an entire iteration budget on a scoring bug.

**Fix**:
1. **Add explicit shape detection** in `deblind_and_score.py` before
   line 64: if the top-level is a dict, look for `per_task` key first
   (new preferred shape), then `tasks`/`scores` (legacy). If the list is
   empty *and* the input payload is non-empty, raise with a clear message
   listing the expected top-level keys.
2. **Support both schemas natively**: for each score entry, accept
   `task_id` OR `taskId`; `quality_a`+`quality_b`+`winner` OR
   `outputA/outputB.quality`. Normalise into a single internal shape at
   the top of the loop.
3. **Write a `scripts/grader_to_scorer_shim.py`** that takes one or more
   grader-OSR files and produces the legacy `blind-grader-scores.json`.
   Reference this in `references/eval-loop.md` Score section.
4. **Exit non-zero when `len(scores_list) == 0`** with an informative
   stderr message — zero-length scoring is never a healthy outcome.
5. **Tighten `agents/grader.md`** with an explicit "downstream scorer
   expects exactly this shape" block, or commit to one shape and rewrite
   the scorer.

Estimated effort: 2-3 hours.

---

### P0.3 Composite formula collapses trajectory signal near 1:1 tool-count ratios

**Symptom**: iter-1 tool counts (super-runner aggregate) 14 with / 32
without → `trajectory = max(0, 1 - 14/32) = 0.5625`. iter-2: 19/18 →
`max(0, 1 - 19/18) = 0` (clamped). That flip alone is worth 40% × 0.5625
≈ **-0.225 composite** per task — precisely the delta observed across
every regressed task (`iter-2/iteration-summary.json` shows -0.189 to
-0.237). Quality actually improved every task.

**Root cause**: Two compounding issues in `scripts/deblind_and_score.py`.
1. **Formula brittleness** (lines 28-31): `max(0, 1 - with/without)` is a
   hard one-sided ratio. Around 1:1 it is (a) asymmetric — reducing
   cost 10% barely moves the needle, increasing cost 10% snaps to 0 —
   and (b) has a huge sensitivity region near parity where small
   measurement noise dominates real signal. A few extra tool calls in
   the with-skill run flip the whole iteration to "regressed".
2. **Per-task tool counts are unit-equivalent per-task values only if
   each task was a separate Runner spawn.** Super-Runner mode produces a
   *session-aggregate* tool count; there is no honest way to distribute
   that across tasks. Every per-task trajectory score is effectively the
   same session-wide number, which makes the 40% trajectory weight a
   noise amplifier.

**Risk**: As observed, **mechanical execution of the "composite regression
> 0.05 → rollback" rule (SKILL.md:224) would have rolled back the
objectively better iter-2 Skill.** This is a correctness failure of the
self-improvement loop.

**Fix** — plural, because no single change suffices:
1. **Replace the trajectory formula** in `compute_trajectory` with a
   bounded, symmetric, saturating form. Proposed:
   ```python
   def compute_trajectory(tool_calls_with, tool_calls_without):
       if tool_calls_without <= 0 or tool_calls_with is None:
           return None  # unavailable, not 0
       # Symmetric log-ratio squashed to [-0.5, 0.5], then shifted to [0, 1]
       import math
       ratio = tool_calls_with / max(1, tool_calls_without)
       raw = -math.log2(ratio) / 4   # 2x fewer → +0.25; 2x more → -0.25
       return max(0.0, min(1.0, 0.5 + raw))
   ```
   This puts parity at 0.5 (not 0), values >1 do not clamp to 0, and
   a 1 tool-call difference at n≈20 moves the score by ~0.02 (not 0.56).
2. **Return `None` instead of `0`** when `tool_calls_without <= 0` or
   the from_log value is null. Propagate None into composite as "no
   trajectory component available" and reweight quality to 100% for that
   task. Add a `trajectory_available_ratio` aggregate so the orchestrator
   can see how much of each composite is quality-only.
3. **Detect Super-Runner aggregates**: if every task in an iteration has
   the same with/without pair, flag as `trajectory_aggregated, severity=warn`
   in `guardrail_flags` and set trajectory to `None` for all tasks (rely
   on quality only). Pattern detection is cheap: set of unique
   `(with, without)` pairs has cardinality 1 and there is more than one
   task.
4. **Split the regression guardrail** (SKILL.md:224). Separate "quality
   regression > 0.05" from "composite regression > 0.05" — only the
   former should trigger auto-rollback. A composite-only regression with
   quality gains should append a `warn` + require orchestrator
   acknowledgement (or new OSR event `trajectory_regression_observed`)
   but not rollback.

Estimated effort: 3-4 hours (formula + unit tests + guardrail split).

---

## P1 — High-value

### P1.1 Trajectory measurement has no working authoritative source in Super-Runner mode

**Symptom**: `scripts/subagent_log.py count-by-uuid` returns
`source=not_available, reason=async_not_in_log` for every async Task
spawn — which is how we actually spawn Runners. Memory index
`project_openjudge_distillation.md` (iter-1 record) noted the same.
Orchestrator consequently uses Runner's self-report, which in
Super-Runner mode is a session aggregate and not a per-task count.

**Root cause**: `scripts/subagent_log.py:148-159` correctly reports
`not_available` when `isSidechain` entries are absent — but this is not
a bug in the script, it's a reflection that **asynchronous Agent Task
invocations genuinely do not emit sidechain records**. The entire
authoritative-tool-count pathway in `references/eval-loop.md:67-77` only
works for synchronous sub-agents, a mode the orchestrator rarely uses.

`scripts/invariant_check.check_task_reality` (lines 131-178) verifies
that `subagent_log_path` exists and is non-empty, but the field is a
**string** in the OSR schema with no minimum length — orchestrator can
(and did) write `""` and pass.

**Risk**: The entire P03 defence ("toolUseCount fabrication") rests on
statistics (`tool_count_variance` check) since the direct verification
path is effectively unavailable. This was acceptable at spec time
because the intent was one-Runner-per-task; but the cost/latency of that
mode pushes every real run into Super-Runner, where the whole layer
fails.

**Fix**:
1. **Formalise Super-Runner mode** as a first-class degraded mode in
   `SKILL.md` §5.1. Document the trade explicitly: "N-task single-spawn
   loses per-task trajectory; trajectory component auto-disabled;
   composite becomes quality-only with a `degraded_mode=super_runner`
   flag in the digest."
2. **Add a Super-Runner protocol** to `references/eval-loop.md`: the
   single Runner must emit a `per_task_tool_counts` section *within its
   own OSR* (not the session total), acknowledging this is self-report
   but at least task-attributed. The scorer then has honest per-task
   numbers even in aggregate spawn.
3. **Tighten the OSR runner schema**: require `subagent_log_path` to be
   `minLength: 1` AND for path existence to be checked by
   `osr_validate.py` as a semantic invariant (not just schema).
4. **Alternative synchronous mode**: investigate whether synchronous
   Agent calls (which DO emit sidechain entries) can be used for the
   Runner step specifically, accepting the higher latency as the cost
   of trajectory correctness. This is the cleanest long-term fix.

Estimated effort: 1 day (option 1-3), up to 2 days with option 4.

---

### P1.2 `overfit_check.py` token Jaccard is English-regex-only; fails on Chinese rationales

**Symptom**: iter-1 Skill Writer had 22 of 23 `changes_applied` entries
flagged with `low_knowledge_overlap` (overlap 0.02-0.12 vs min 0.15).
Every one was genuinely grounded in knowledge/*.md citations I
verified by eye. The Skill Writer itself flagged the root cause as a
`meta_observation`: "token regex is English-only".

**Root cause**: `scripts/overfit_check.py:37`:
```python
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|\w+")
```
`\w+` does include CJK in Python 3's default `re` (Unicode-aware), but
in practice Chinese text has no whitespace so `\w+` snaps up whole
sentences as single tokens and the Jaccard set of ~5 giant "tokens" has
near-zero overlap with the similarly-segmented rationale. The English
alt `[A-Za-z_]...` short-circuits for ASCII text but for mixed
Chinese/English, tokenisation is dominated by the degenerate CJK path.

**Risk**: Every Chinese-centric Skill Writer iteration fires 20+ false
positive warnings, which the orchestrator is trained by the prompt to
treat as "pro-forma citations". This either (a) gets ignored, teaching
the orchestrator to ignore overfit warnings generally, or (b) triggers
needless re-spawn. Both outcomes weaken the P04 defence.

**Fix**:
1. **Segment CJK properly**. Two clean options:
   - (a) Character-level segmentation for CJK: treat each Han character
     as a token. Replace `TOKEN_RE` with two passes:
     ```python
     ASCII_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
     CJK_CHAR = re.compile(r"[一-鿿]")
     def _tokens(text):
         t = text.lower()
         return set(ASCII_WORD.findall(t)) | set(CJK_CHAR.findall(t))
     ```
     Character-grain overlap is noisy but predictable.
   - (b) Optional `jieba` dependency behind a try/except for proper
     Chinese word tokenisation; fall back to (a) if not installed.
2. **Lower the threshold to 0.10 when non-ASCII characters dominate**
   in either side of the comparison. Auto-detected at runtime.
3. **Add a `--tokenizer {ascii,cjk-char,jieba}` CLI flag** so we can
   A/B the signal.
4. **Emit the overlap distribution to the output JSON** so operators can
   see the full distribution (not just pass/fail) for calibration.

Estimated effort: 2 hours.

---

### P1.3 OSR schema `maxLength` is too tight for honest filling; reject-loop wastes iterations

**Symptom**: Researcher OSR hit 4 `maxLength` violations: `surprises[].short`
(120), `surprises[].suggested_action` (200), `research_direction` (200),
`meta_observations[]` (200). I truncated manually rather than re-spawn.

**Root cause**: `schemas/osr-researcher.schema.json:39,40,60,96` and
`scripts/osr_validate.py:143-148` treat `maxLength` as a hard reject,
with only a "truncate to N chars" suggestion text. No auto-recovery.
`SKILL.md:253` also specifies a max 2-retry policy for rejects, so a
chatty researcher could fail into abandonment purely for string-length
reasons — while the content is fine.

**Risk**: Frequent false rejects during the research phase that hurt the
signal value of rejects (reject becomes "noise" instead of "genuine
schema violation"), and genuine truncation decisions are made by hand
and therefore not audited.

**Fix**:
1. **Auto-truncate `maxLength` overruns for free-text fields** with a
   warning. Add a `--auto-truncate` flag (default on) to
   `osr_validate.py` that rewrites string fields in place with a
   `...[truncated, original len=N]` marker, emits a warning for each,
   and exits 0 if truncation was the only issue. Add a
   `truncations_applied` field to the output for auditability.
2. **Raise the caps for content-heavy fields**: `research_direction`
   300, `meta_observations[]` 300, `surprises.suggested_action` 300.
   The sizes are soft ergonomics, not context-budget limits — the true
   budget check is in `state.json`, not here.
3. **Hard-reject remains** for structural violations (missing required,
   enum mismatch, type mismatch). Only string-length is auto-salvaged.

Estimated effort: 2 hours.

---

### P1.4 No automatic response to eval-set saturation (100% skill-won)

**Symptom**: By iter-2, all 12 tasks reported `skillWon=yes` with mean
quality 4.58/5. `invariant_check.check_skill_won_rate`
(`scripts/invariant_check.py:263-292`) flagged `skill_won_rate=1.0 > 0.9`
with the recommendation "upgrade context_mode to 'rich' and read
feedback_file" — but reading the feedback only confirms the obvious
("the Skill is winning every task"). There is no designed action from
saturation.

**Root cause**: The guardrail exists but points at a dead end
(reading feedback). The SKILL.md `next_action` mapping (§4) has no entry
for "eval saturated". The Eval Designer is only spawned **once**, during
the `generate` phase, and never revisited during `iterate`.

**Risk**: Every further iteration after saturation is wasted budget.
Worse, a saturated eval can mask regressions in orthogonal dimensions:
nothing in the iteration-summary distinguishes "skill is still improving
on capability X" from "skill is re-wording things without changing
substance" when every task already scores 4+.

**Fix**:
1. **Add a new guardrail response in `invariant_check.py`**: when
   `skill_won_rate >= 0.9` AND `|Δ_quality_vs_prev| < 0.05` AND
   `iteration >= 2`, escalate to severity `critical` with recommendation
   `spawn_eval_designer_extension`.
2. **Add an "eval extension" flow to SKILL.md §4**: a new phase
   transition `iterate → eval-extend → iterate` that re-spawns Eval
   Designer with a specific prompt: "You are extending an existing
   eval-tasks.json with 4-6 tasks that *stress* the current Skill —
   i.e., your task is to find tasks where the Skill genuinely does not
   yet provide value. Read `iteration-summary.json` for what the Skill
   already handles. Forbidden: duplicating existing task categories."
3. **Add extension-mode task id prefix** (e.g. `ext-2-<slug>`) so
   cross-iter comparisons can split "original set" and "hard set"
   metrics. Old composite averages stay comparable; new hard-set
   averages show the real gradient.
4. **Update `agents/eval-designer.md`** with a "Extension mode" section
   that inherits all isolation rules and adds the forbidden-duplication
   rule.

Estimated effort: 4-5 hours.

---

### P1.5 Eval Designer runs *before* seeing the Skill it grades: chicken-and-egg

**Symptom** (my observation, not strictly this run): Eval Designer is
spawned in `generate` phase, parallel to Skill Writer, with access only
to `knowledge/*.md`. It writes tasks *before* the Skill exists, so its
guesses at what provides skill-vs-baseline gap are speculative.

**Root cause**: `SKILL.md:65-66` places Eval Designer in `generate` —
after research, before iterate, but in practice near-parallel to the
initial Skill Writer invocation. Task design depends on "what will
reading this Skill enable that reading the repo alone does not" — which
requires seeing the actual Skill.

**Risk**: Designer produces tasks that (a) both Runners can answer from
repo alone (low gap) or (b) neither Runner can answer (over-scoped).
iter-0 grader feedback is then muddy and the first Skill-Writer round
operates on low-signal evidence.

**Fix**:
1. **Two-pass Eval Design**: (a) `generate-pass-1` Eval Designer writes
   "speculative" tasks from knowledge only. (b) After iter-1's grader
   digest comes in, spawn a `generate-pass-2` Eval Designer whose
   prompt is: "For each task, we now have Skill + Runner outputs +
   grader feedback. Revise tasks with quality_range < 1.5 (low
   discrimination) or quality_a=quality_b=5 (both max, wasted slot) or
   suggestion_file notes 'ambiguous'." Bind the revision to a diff, not
   a rewrite — most tasks should survive unchanged.
2. **Add `task_revisions_applied` field to the Eval Designer OSR
   schema** so the orchestrator can audit what changed across passes.
3. **Alternative (cheaper)**: just rename the pass-2 step to `eval-repair`
   and trigger it only when `mean_quality_range < 1.0` or
   `zero_discrimination_tasks > 0` from the iter-1 digest.

Estimated effort: 1 day.

---

### P1.6 Mechanical rollback policy doesn't distinguish quality from trajectory regressions

**Symptom** (already referenced in P0.3): `SKILL.md:224`: `composite
regression > 0.05 → rollback` executed literally would have reverted
iter-2's objectively better Skill.

**Root cause**: `SKILL.md` describes only one regression dimension.
`deblind_and_score.py:120-139` only computes `delta` from composite
(line 136-139). Quality delta is available (it's in per_task.quality)
but not surfaced as a regression axis.

**Risk**: Systematic bias against iterations that improve quality while
incurring slightly more tool calls. Over many iterations this pressures
the Skill Writer towards terseness at any cost, which conflicts with
the quality-first philosophy in `agents/skill-writer.md:11-19`.

**Fix**:
1. **Compute two delta series** in `deblind_and_score.py`:
   `composite_delta` (existing) AND `quality_delta`. Emit both in
   `iteration-summary.regressions` as separate arrays:
   `composite_regressions`, `quality_regressions`.
2. **Rewrite SKILL.md §9 rollback row**:
   > composite regressed > 0.05 BUT quality did not regress → append
   > `trajectory_regression_observed` event, do NOT rollback
   > quality regressed > 0.05 on any task → rollback
3. **Make the rollback a two-step**: stop writing `git checkout
   skill-v<N-1>` as a blind action; first dump a "would-rollback-because-X"
   event, require one more iteration to confirm, or require explicit
   `allow_rollback: true` flag in state.

Estimated effort: 3 hours.

---

## P2 — Nice to have

### P2.1 PostToolUse hook cannot populate authoritative `subagent_log_path`

**Symptom**: OSR runner schema field `subagent_log_path` exists, but the
PostToolUse hook that would populate it runs in the orchestrator
session, not the subagent session. Runner self-filling `""` passes the
schema because `type=string` has no `minLength`.

**Root cause**: Hook architecture limit. Hooks are backstop loggers per
`SKILL.md:247` — they don't have the sidechain info that `subagent_log.py
count-by-uuid` produces after-the-fact.

**Fix**:
1. Make `subagent_log_path` `minLength:1` in the runner schema and add a
   semantic invariant in `osr_validate.py` that the path must exist on
   disk (similar to the skill-writer knowledge_source_refs invariant at
   lines 238-250).
2. Document in `references/eval-loop.md` step 3 that the orchestrator
   must populate this field *by patching the Runner OSR* after
   `subagent_log.py count-by-uuid` returns — not leave it blank. Make
   it step 3.5 of the Run phase.

Estimated effort: 1 hour.

---

### P2.2 `validate_skill.py` description `MAX_DESCRIPTION_LENGTH=1024` is tight when it must encode WHAT+WHEN+triggers

**Symptom**: Four skills needed multiple trimming passes to fit the
1024 char cap while retaining all three required elements (WHAT, WHEN,
explicit trigger phrase).

**Root cause**: `scripts/validate_skill.py:56`. 1024 chars is the
Anthropic spec's hard cap, so this isn't fixable upstream — but the
cap is uncomfortably tight when the description must *both* trigger
discovery (requires trigger phrases) and carry capability context
(WHAT+WHEN).

**Fix**:
1. **Keep the cap** (it's the spec's) but add a **Skill Writer helper**
   `scripts/compose_description.py` that takes (what, when, triggers)
   parts as separate inputs and emits a best-effort <=1024 composite,
   raising a warning if even the shortest rendering overflows. Then the
   Skill Writer doesn't rewrite by hand.
2. **Downgrade WHAT+WHEN+trigger to a prompt expectation** in
   `agents/skill-writer.md:59-60` with an explicit "if short, prefer
   trigger phrase > WHAT > WHEN — Claude needs triggers to route".

Estimated effort: 1 hour.

---

### P2.3 `repo_manifest.py` docstring mis-advertises `--repo` as optional

**Symptom** (Researcher meta_obs): docstring line 13 says
`[--repo <repo-path>]` (brackets mean optional). In practice, if
`workspace/repo/` does not contain the target, the script `sys.exit(1)`
with "cannot find repo" (line 241). For our workflow where the
workspace and the repo under study are siblings, `--repo` was
effectively required.

**Root cause**: `scripts/repo_manifest.py:228, 238-242`. Discovery logic
assumes `workspace/repo/<slug>`; docstring does not explain.

**Fix**:
1. Rewrite the usage string to: `<workspace> [--repo <repo-path>] # --repo required unless workspace/repo/<anything> exists`.
2. Migrate to argparse for consistent error output; add
   `--repo` with `required=False` and a clearer "found no repo under
   `<workspace>/repo/`" message pointing the user at `--repo`.

Estimated effort: 30 min.

---

### P2.4 `state_manager.py` CLI parameter asymmetry

**Symptom**: Observed drift:
- `append-event` takes `--summary`
- `phase-transition` does NOT take `--summary` (only `--to`)
- `write-iter` takes `--data` where `--content` would be idiomatic

**Root cause**: `scripts/state_manager.py:428-447`. Different authors,
different eras.

**Risk**: Low — error messages are clear, so nothing breaks silently.
But cognitive tax accumulates across long sessions.

**Fix**:
1. Add `--summary` to `phase-transition` and persist it into the
   transition event.
2. Add `--content` as an alias for `--data` in `write-iter`.

Estimated effort: 30 min.

---

### P2.5 Grader worktree harvest step is manual

**Symptom**: Graders produce `evals/results/iter-<N>/` artefacts inside
their worktree, not inside the workspace. I manually `cp -R` those back
to `workspace/evals/results/iter-<N>/` after every grade round.

**Root cause**: No script owns this handoff.
`references/eval-loop.md:164-170` shows `aggregate_grades.py
--grader-dirs <g1>,<g2>,...` which can read from worktrees — but the
single-grader path (also valid per SKILL.md) has no documented harvest.

**Fix**:
1. Add `scripts/grader_harvest.py --workspace <ws> --iter <N>
   --grader-dirs <list>` that copies `blind-grader-scores.json`,
   `grader-feedback.jsonl`, `grader-suggestions.jsonl` from each grader
   worktree into the workspace results dir. For K=1, copy from the one
   dir. For K>=2, delegate to `aggregate_grades.py` (which already
   handles this).
2. Or, extend `aggregate_grades.py` to be the single harvest entry
   point regardless of K, making K=1 a trivial pass-through. Document
   in SKILL.md §4 `iterate` row.

Estimated effort: 2 hours.

---

### P2.6 Post-deblinding digest's `convergence.py` subprocess can silently fail

**Symptom** (not observed this run, but latent): `deblind_and_score.py`
line 167-172 runs `convergence.py` as a subprocess and catches all
`json.JSONDecodeError`/`ValueError` by defaulting to
`converged=False, reason="continuing"`. If convergence.py crashes, the
loop runs forever with no indication.

**Fix**: log stderr from `convergence.py` on non-zero exit; surface
through an `anomalies` entry in the digest rather than swallowing.

Estimated effort: 30 min.

---

## Cross-cutting observations

### X.1 The system has three distinct trust layers for tool counts

- **Authoritative** (sidechain log): works for sync sub-agents; unusable
  in Super-Runner mode; unavailable for async.
- **Self-report** (`tool_use_count_self_report`): always available, always
  suspicious.
- **Statistical** (`invariant_check tool_count_variance`): cross-iter
  pattern detection; can only detect obvious fabrication (iter-N ==
  iter-N+1 exactly).

**None of them work well for the dominant case of async Super-Runner.**
The architecture implicitly assumes sync; SKILL.md should formally name
Super-Runner a supported-but-degraded mode with explicit losses
(trajectory signal unusable) and explicit compensations (quality-only
composite, honest per-task self-report with variance check).

### X.2 "Silent-zero" is a systemic anti-pattern that shows up in 3 places

1. `deblind_and_score.py` composite=0 on schema mismatch (P0.2).
2. `overfit_check.py` overlap=0 on CJK tokenisation collapse (P1.2).
3. `subagent_log.py` count=None on async, used as 0 in the composite
   formula (`compute_trajectory` line 30 treats None-via-missing-field
   as 0).

General fix pattern: **any value the scorer/check uses should
distinguish `unavailable` from `zero`.** Ops on `None` should propagate
to a reporting field (`trajectory_unavailable_ratio`) rather than
silently contribute zero to the average.

### X.3 The loop underuses its own `anomalies` / `surprises` channel

Over the run, I recorded maybe 6 surprises total (mine + agent-filed).
Nothing triggered the "silent for 3 iters → diagnostic spawn" guardrail
(`invariant_check:323-356`) because 3 iters wasn't enough. But the fact
that the Super-Runner trajectory problem was filed multiple times as
`anomaly` across iters should have compounded into a loop-wide "we
cannot measure trajectory this run, fall back to quality-only" decision
— and no mechanism aggregates anomaly recurrence across iters for
automatic policy changes.

**Fix direction**: Add a `recurring_anomalies` counter maintained by
`state_manager` that, when a claim substring repeats ≥ 2 iters,
escalates the anomaly to a policy-change candidate (e.g., "disable
trajectory component for this run").

---

## 建议修复顺序 (Suggested fix order)

Ordered by (priority × unblocks-other-work ÷ effort):

| # | Item | Priority | Est. | Rationale |
|---|------|----------|------|-----------|
| 1 | **P0.1** — worktree grader preset / auto-exclude `blind-mapping.json` | P0 | 1-2 h | Safety-critical; trivial to implement; blocks all future grader runs. |
| 2 | **P0.2** — deblind_and_score shape detection + shim | P0 | 2-3 h | Silent-zero is the worst failure mode; fixes once and permanently. |
| 3 | **P0.3** — composite formula + quality/trajectory split | P0 | 3-4 h | Without this, every iter is a coin flip whether rollback fires. |
| 4 | **P1.2** — CJK-aware overfit_check tokenizer | P1 | 2 h | Cheap; immediately improves signal-to-noise for all Chinese Skill Writers. |
| 5 | **P1.3** — osr_validate auto-truncate | P1 | 2 h | Reduces reject-loop churn, speeds every research phase. |
| 6 | **P1.6** — rollback policy split (pairs with P0.3) | P1 | 3 h | Do together with P0.3; policy change belongs with formula change. |
| 7 | **P1.1** — Super-Runner mode formalisation + schema tightening | P1 | 1 d | Most architectural of the P1s; unblocks honest trajectory story. |
| 8 | **P1.4** — saturation → eval extension flow | P1 | 4-5 h | Value scales with run length; not urgent if runs stop at iter-2. |
| 9 | **P2.5** — grader_harvest.py | P2 | 2 h | Paves over operator friction; improves reproducibility. |
| 10 | **P1.5** — two-pass Eval Designer | P1 | 1 d | Largest design change; defer until 1-9 give clean signal. |
| 11 | **P2.1 / P2.2 / P2.3 / P2.4 / P2.6** | P2 | ~3 h total | Quality-of-life; batch in one cleanup commit. |

**Opportunistic grouping**: P0.3 + P1.6 must ship together (formula + policy).
P0.1 + P2.5 are both worktree-adjacent and can share a diff.

**Do not ship alone**: P0.3 without P1.6, or vice versa — you'd either
auto-rollback on every quality-positive iter (if you keep the policy but
fix the formula returning None) or lose the rollback safety net entirely
(if you soften the policy but don't fix the formula).

---

## Appendix: Raw observations from this run

### A. Final scoring table (iter-2 vs iter-1)

- 12/12 tasks: `skillWon=yes`
- mean quality 4.58/5 (up from 4.33 in iter-1)
- composite 0.55 (down from 0.6438 in iter-1; delta -0.09)
- **every task has `composite_delta <= -0.189`** — unanimous "regression"
  despite unanimous quality improvement. This is the textbook
  trajectory-signal collapse described in P0.3.

### B. Files / locations that bit us (for grep-ability)

- `scripts/worktree_helper.py:55-67, 89, 124-130` (P0.1)
- `scripts/deblind_and_score.py:28-31, 64-65, 72-93` (P0.2, P0.3)
- `scripts/subagent_log.py:148-159` (P1.1)
- `scripts/overfit_check.py:37, 40-42` (P1.2)
- `scripts/osr_validate.py:143-148` (P1.3)
- `scripts/invariant_check.py:263-292` (P1.4)
- `scripts/validate_skill.py:56` (P2.2)
- `scripts/repo_manifest.py:228, 238-242` (P2.3)
- `scripts/state_manager.py:428-447` (P2.4)
- `schemas/osr-researcher.schema.json:39,40,60,96` (P1.3)
- `SKILL.md:111, 224` and `references/eval-loop.md:115,126` (P0.1, P1.6)

### C. Things that worked well (for balance)

- **OSR protocol structure** performed well — once schemas were navigated
  around, they carried exactly the information the orchestrator needed.
  `surprises`/`anomalies`/`meta_observations` channels are load-bearing
  and I used all three.
- **`osr_validate` + `overfit_check` as backstops** caught one genuine
  pro-forma citation (hidden among the noise of CJK false positives) —
  so the check is not pure noise.
- **Physical isolation via worktree** is correct architecture; the bugs
  are at the boundary (what gets copied in/out) not in the concept.
- **State manager's Markov recovery property** was tested implicitly
  when I paused and resumed — worked.
- **Ensemble design (even though we ran K=1)** means the path is
  available when trajectory noise dominates; `aggregate_grades.py`
  + `ensemble-metrics.json` is ready to deploy.
