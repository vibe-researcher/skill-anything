---
name: openjudge-grader-selection
description: >-
  Use this skill when picking an OpenJudge grader, deciding LLM-as-judge vs
  deterministic scoring, choosing score scales, aggregating multiple graders,
  or debugging silent parse errors. Triggers include "which grader",
  "LLMGrader vs FunctionGrader vs AgenticGrader", "grader for correctness /
  relevance / hallucination / tool-call / code / math", "composite score",
  "WeightedSumAggregator", "normalize scores", "structured_model",
  "voting / average strategy", pointwise vs listwise. Covers a decision
  tree over the four grader base classes, scenario-to-concrete-grader lookup,
  the full score-scale map (TrajectoryAccuracyGrader is 1-3,
  TrajectoryComprehensiveGrader is 0-1, common graders 1-5, single-step
  agent graders mostly binary), LLMGrader internals (three-tier fallback,
  silent Qwen/Gemini/pai-judge schema downgrade, template traps), and
  evaluation strategies. Do NOT trigger for rubric generation
  (use openjudge-rubric-workflow) or agent granularity
  (use openjudge-agent-eval).
version: "1.0.0"
license: MIT
---

# OpenJudge Grader Selection

Goal: help you pick the right grader for a scoring goal, configure it so the
returned score means what you think it means, and aggregate multiple graders
without producing silent garbage.

If you only remember two things:

1. **`GraderScore.score` is an unconstrained float** (`openjudge/graders/schema.py:92` — `score: float` with no validator). Different graders use different scales. `WeightedSumAggregator` does not check. Always normalize to [0, 1] before aggregating.
2. **LLM judges silently downgrade.** Qwen / Gemini / pai-judge strip your Pydantic schema (`openjudge/models/openai_chat_model.py:239-246`). A three-tier fallback chain hides prompt misbehavior. Always clamp scores client-side and monitor fallback warnings.

**Discipline: name-check every class against the actual `openjudge` tree before shipping.** Plausible-looking names (e.g. "CorrelationGrader") slip through — verify imports against `openjudge/graders/` before writing them down.

## The four grader base classes

Every concrete grader is one of these.

| Class | Use when | Cost | Stochastic? |
|---|---|---|---|
| `FunctionGrader` | Signal is computable deterministically from data alone (exact match, regex, tolerance check, JSON schema check, set intersection, syntax check, AST parse). | Near-zero | No |
| `LLMGrader` | Signal is subjective, semantic, or open-ended — needs a judge LLM. ~80% of built-ins. | 1 LLM call per sample (more with strategies) | Yes |
| `AgenticGrader` | The judge itself needs to verify facts externally (web search, code execution, API calls, citation existence). | `max_iterations` * (LLM + tool) per sample — 10-100x `LLMGrader` | Yes |
| `BaseGrader` | Never subclass directly — pick one of the three concrete classes. | — | — |

### Decision tree

```
Is the signal computable deterministically from data alone?
├─ YES → FunctionGrader (or a built-in code-based grader)
└─ NO → Is the judgment a function of the given text/trajectory?
        ├─ YES → LLMGrader (built-in if one matches; else custom template or
        │        generated via Rubric generators — see openjudge-rubric-workflow)
        └─ NO — needs external verification
                 → AgenticGrader with a pre-built ReActAgent + tools
```

### `AgenticGrader` has changed

Since v0.2.0 "unified interface" refactor, `AgenticGrader` **no longer**
accepts `model=...` / `tools=[...]` in the constructor. You must build the
agent first:

```python
from openjudge.agentic import ReActAgent
from openjudge.graders.agentic_grader import AgenticGrader

agent = ReActAgent(model={"model": "gpt-4"}, tools=[WebSearchTool()], max_iterations=10)
grader = AgenticGrader(agent=agent, template="Evaluate: {response}")
```

Older tutorials show `AgenticGrader(model=..., tools=[...])`. That path is dead.

## Concrete-grader lookup — think by **goal**, not by module

| Goal | Grader | Scale |
|---|---|---|
| Exact / fuzzy string match | `StringMatchGrader` / `FunctionGrader` | binary or [0, 1] |
| Semantic similarity | `SimilarityGrader` (embedding) | [0, 1] |
| Factual correctness vs reference | `CorrectnessGrader` | 1-5 |
| Hallucination / groundedness | `HallucinationGrader` | 1-5 (5 = not hallucinatory; do NOT invert sign) |
| Query relevance | `RelevanceGrader` | 1-5 |
| Instruction adherence | `InstructionFollowingGrader` | 1-5 |
| Safety | `HarmfulnessGrader` / `SafetyGrader` | 1-5 |
| Tool-call correctness | `ToolCallAccuracyGrader` (LLM) / `ToolCallPrecisionRecallMatchGrader` (code) | 1-5 / [0, 1] |
| Tool selection quality | `ToolSelectionGrader` | 1-5 |
| Plan feasibility | `PlanFeasibilityGrader` | binary {0, 1} |
| Memory accuracy | `MemoryAccuracyGrader` | binary |
| Reflection accuracy | `ReflectionAccuracyGrader` | binary |
| Trajectory success (coarse) | `TrajectoryAccuracyGrader` | **1-3** |
| Trajectory (per-step, normalized) | `TrajectoryComprehensiveGrader` | [0, 1] |
| Code quality | `CodeComplexityGrader` / `CodeSecurityGrader` / `CodeBugDetectionGrader` | 1-5 |
| Code correctness (runtime) | `CodeExecutionGrader` | [0, 1] |
| Math | `MathExpressionVerifyGrader` | binary |
| Custom criteria, no data | `SimpleRubricsGenerator` → `LLMGrader` | configurable |
| Custom criteria, labels available | `IterativeRubricsGenerator` → `LLMGrader` | configurable |
| Needs web / code verification | `AgenticGrader` + `ReActAgent` | custom |

See `openjudge-agent-eval` for the agent-specific granularity and cognitive-module axes.

## The score-scale map — memorize or keep nearby

`GraderScore.score: float` has **no validator**. Each grader decides its scale via the prompt's `<Scale>` block, an optional `structured_model` Pydantic constraint, and an optional post-processing callback. The three do not always agree.

### Common / multi-turn / skills / code-LLM graders — **1-5 integer**

All of these: higher = better. Inversion is **not** needed even for negative-sounding names like `HallucinationGrader` or `ResponseRepetitionGrader` — 5 means "not hallucinatory" / "not repetitive".

`RelevanceGrader`, `CorrectnessGrader`, `HallucinationGrader`, `HarmfulnessGrader`, `InstructionFollowingGrader`, `SearchCorrectnessGrader`, `TopicSwitchGrader`, `SelfCorrectionGrader`, `AnaphoraResolutionGrader`, `ResponseRepetitionGrader`, `ProactiveInteractionGrader`, `ContextMemoryGrader`, `InstructionClarificationGrader`, skills-package graders (`CompletenessGrader`, `ComprehensiveGrader`, `SafetyGrader`, `StructureGrader`), code-LLM graders (`CodeComplexityGrader`, `CodeStyleGrader`, `CodeBugDetectionGrader`, `CodeSecurityGrader`).

### Agent graders — **heterogeneous, four distinct scales**

This is where bugs breed.

| Scale | Graders |
|---|---|
| **Binary {0.0, 1.0}** | `ActionAlignmentGrader`, `ToolCallSuccessGrader`, `ToolParameterCheckGrader`, `MemoryAccuracyGrader`, `MemoryDetailPreservationGrader`, `MemoryRetrievalEffectivenessGrader`, `PlanFeasibilityGrader`, `ReflectionAccuracyGrader`, `ReflectionOutcomeUnderstandingGrader`, `ReflectionProgressAwarenessGrader` |
| **Continuous [0.0, 1.0]** | `ActionLoopDetectionGrader`, `ObservationInformationGainGrader`, `ToolCallStepSequenceMatchGrader`, `ToolCallPrecisionRecallMatchGrader`, `TrajectoryComprehensiveGrader` |
| **1-5 integer** | `ToolSelectionGrader`, `ToolCallAccuracyGrader` |
| **1-3 integer** | `TrajectoryAccuracyGrader` — the shortest scale in the library |

`TrajectoryAccuracyGrader` (1-3) and `TrajectoryComprehensiveGrader` (0-1) share a name space but use **different scales**. If they disagree wildly in a pipeline, scale mismatch is the first hypothesis.

### Code-based / other

- `MathExpressionVerifyGrader` — binary. Code-based; handles symbolic equivalence.
- `StringMatchGrader`, `NumberAccuracyGrader`, `SimilarityGrader` — [0, 1], code-based.
- `SyntaxCheckerGrader`, `PatchSimilarityGrader` — binary / [0, 1], code-based.
- JSON/format graders — binary.
- `CodeExecutionGrader` — [0, 1], code-based (runs tests).
- Generated-rubric graders — **configurable**: `SimpleRubricsGeneratorConfig` and `IterativePointwiseRubricsGeneratorConfig` take `min_score` (default 0) and `max_score` (default 1). Defaults are [0, 1] **integer**. If your downstream aggregation assumes a different range, pass `min_score=1, max_score=5` explicitly.

## Aggregation — the only safe rule

`WeightedSumAggregator(weights=...)` multiplies score by weight, sums, and
divides by the sum of applied weights (see `openjudge/runner/aggregator/weighted_sum_aggregator.py:62-82`).
It does **not** check scales, does **not** record expected ranges, and does
**not** warn when values are out-of-range. Two silent failure modes:

**S1 — Mixed scales produce meaningless composites.** Worked example — four graders at equal weight 0.25, raw scores `correctness=4` (1-5), `tool_sel=5` (1-5), `trajectory=0.7` (0-1), `plan=1.0` (binary):

```
0.25*4 + 0.25*5 + 0.25*0.7 + 0.25*1.0 = 2.675
```

That number looks like "~2.7 / 5" but the 1-5 graders contribute `0.25*4 + 0.25*5 = 2.25` — about **84% of the composite** — while the binary and [0,1] graders together contribute `0.425`. Adjusting the binary/continuous weights barely moves 2.675. Users misattribute this to "grader noise". The 1-5 graders dominate by construction.

Normalized (all to [0, 1]): `0.25*0.75 + 0.25*1.0 + 0.25*0.7 + 0.25*1.0 = 0.8625`. Now each dimension contributes its intended 25%.

**S2 — `GraderError` leaves the denominator.** A sample where `hallucination`
errored (weight 0.4) gets its composite computed over only the surviving
graders — weight re-normalized over the survivors (the `if isinstance(result, GraderScore)` guard at `weighted_sum_aggregator.py:66` excludes errors from both numerator and denominator). Different samples can have different surviving sets. Cross-sample comparisons become invalid, and the aggregator does not record which graders were skipped per sample. **Default policy:** substitute a neutral/worst-case value for errors, or write a custom aggregator that returns `GraderError` if any input errored. Never silently drop.

### Safe aggregation options, preferred to least preferred

**A. Use only already-normalized graders.** Code-based agent graders
(`ToolCallStepSequenceMatch`, `ActionLoopDetection`, `ObservationInformationGain`,
`TrajectoryComprehensiveGrader`) and text code-based graders already emit [0, 1].

**B. Wrap each raw grader in a `FunctionGrader` normalizer.**

```python
from openjudge.graders.function_grader import FunctionGrader
from openjudge.graders.schema import GraderMode, GraderScore, GraderError

def make_normalizer(inner, lo, hi, new_name):
    async def norm(**kwargs):
        r = await inner.aevaluate(**kwargs)
        if isinstance(r, GraderError):
            return r
        clamped = max(lo, min(hi, float(r.score)))  # clamp BEFORE normalizing
        return GraderScore(name=new_name, score=(clamped - lo) / (hi - lo),
                           reason=r.reason, metadata={**r.metadata, "original_score": r.score})
    return FunctionGrader(func=norm, name=new_name, mode=GraderMode.POINTWISE)
```

Normalization formulas — **say the denominator out loud every time**:

```
1-5 → [0, 1]:  (x - 1) / 4   ← denominator 4, not 5 (range span)
1-3 → [0, 1]:  (x - 1) / 2   ← denominator 2, not 3
binary, [0,1], continuous normalized: no change
min-max int:   (x - min) / (max - min)
```

The two trajectory-family graders are the single most common accident: `TrajectoryAccuracyGrader` is 1-3, `TrajectoryComprehensiveGrader` is [0,1]. Copy-pasting the `/4` from a neighboring 1-5 grader silently squashes 1-3 scores into `[0, 0.5]`. If the denominator doesn't feel natural, you're probably on the wrong scale.

**Worked sibling-trap example.** Pipeline with `TrajectoryAccuracyGrader` (1-3) at weight 0.5 and `TrajectoryComprehensiveGrader` ([0,1]) at weight 0.5. Accuracy mean = 2.4 (typical healthy), Comprehensive mean = 0.65:

```
raw composite:       0.5 * 2.4 + 0.5 * 0.65 = 1.525
Accuracy contributes: 1.2   (~79%)
Comprehensive contributes: 0.325   (~21%)
```

The 1-3 sibling is **3.7x louder** than its 0-1 sibling — the grader you intended as 50-50 is effectively 4-to-1.

**Always clamp before normalizing** — the judge can return out-of-range values (see LLMGrader traps below).

**Operational rule: preference order for handling scale mismatch is drop > normalize > always-smoke-test.** If you can drop the redundant-signal grader (e.g., both trajectory siblings), do that first; normalizing keeps two correlated columns. Smoke-test regardless.

### Cost tactic: LLM only on the tail

When a deterministic/code-based grader covers 80%+ of cases, run the LLM grader *only on the low-scoring tail* (e.g., `code_score < 0.5`) rather than the full set. Typical saving: 5-10x judge cost with no loss of signal on the high-scoring body — which you already know is fine.

**C. Custom aggregator.** Subclass `BaseAggregator` and carry a `(min, max)` table; return `GraderError` (or a worst-case score) when any input errored, rather than silently dropping.

## LLMGrader internals — traps you need to know

The `_aevaluate` flow in six steps: merge kwargs → render template → call model → maybe collapse stream → extract parsed with three-tier fallback → build `GraderScore` or `GraderRank`. Every step has a trap.

### Trap 1 — Silent `structured_model` downgrade

`openjudge/models/openai_chat_model.py:239-246`:

```python
if "qwen" in self.model.lower() or "gemini" in self.model.lower() or "pai-judge" in self.model.lower():
    logger.info(f"Model '{self.model}' detected: Automatically switching to 'json_object' response_format")
    structured_model = {"type": "json_object"}
```

If you pass `structured_model=GraderScoreCallback` with `ge=1, le=5` and your
model is Qwen/Gemini/pai-judge, the **entire Pydantic schema is discarded**. The
provider gets only "return some JSON". The model can return `{"score": 8}` or a
string `"score": "great"` — your `ge/le` is never enforced server-side. The only signal is a `loguru` INFO line; nothing raises.

Mitigations:

- Always clamp in a callback or post-hoc: `score = max(lo, min(hi, float(score)))`.
- Explicitly describe the expected JSON shape in the prompt — most OpenJudge built-ins already do: `{"reason": "...", "score": <integer 1-5>}`.
- Prefer OpenAI- or Anthropic-hosted judges for schema-critical evals.
- Grep logs for `"Automatically switching to 'json_object'"` — that is the downgrade firing.

### Trap 2 — Default `structured_model` is set silently

`openjudge/graders/llm_grader.py:178-182` — if you pass `structured_model=None`, the constructor auto-assigns `GraderScoreCallback` (POINTWISE) or `GraderRankCallback` (LISTWISE). Both have `score: float` / `rank: List[int]` + `reason: str` + `metadata: dict`. **No `ge/le`.** "I didn't set structured_model, so there's no schema" is wrong — there is one, just permissive.

For any grader with a scoped score range, pass an explicit `structured_model` with `Field(ge=min_score, le=max_score)`.

### Trap 3 — The three-tier fallback chain hides prompt misbehavior

`LLMGrader._aevaluate` tries, in order:

1. **Structured parse from SDK.** Succeeds when provider honors your schema.
2. **Embedded-JSON regex.** Walks every `{...}` substring, finds the first with `"score"` or `"rank"`. Logs `"recovered via embedded-JSON fallback"`.
3. **Free-text regex.**
   ```
   r'(?:score|rating|分数|得分)["\s:：]*(\d+(?:\.\d+)?)'
   r'(\d+(?:\.\d+)?)\s*(?:out\s+of|/)\s*\d+'
   r'(?:give|assign|rate|评分)[^0-9]*(\d+(?:\.\d+)?)'
   r'(?:^|\n)\s*(\d+(?:\.\d+)?)\s*(?:\n|$)'
   ```
   Logs `"recovered via regex fallback"`. These patterns match things you don't want — "references 5 people" can register as score 5 when Tier 1 parses nothing. So even "recovered" scores may be wrong, not merely late.
4. **`GraderError`.** All three failed; `reason` contains raw content.

A prompt that consistently emits malformed JSON "works" — just on Tier 3 guesses. Quality drifts silently for months.

**Monitoring discipline (standard log-grep audit for every pipeline run):**

```bash
grep "recovered via embedded-JSON fallback" logs/*.log | wc -l
grep "recovered via regex fallback"          logs/*.log | wc -l
grep "Automatically switching to 'json_object'" logs/*.log | wc -l
```

Per-grader fallback frequency is a prompt-quality signal — anything above ~1-2% means fix the prompt, not retry. The `json_object` line is the downgrade firing on Qwen/Gemini/pai-judge. Wire both into CI/alerting.

### Trap 4 — `self.kwargs` collisions, Python `.format`, bilingual templates, lossy config

Four mechanically-different but practically-adjacent traps:

- **kwargs collide.** `_aevaluate` merges `self.kwargs` with runtime kwargs via `dict.update`; runtime wins. Don't pass constructor-level keys (`min_score`, `rubrics`, …) as runtime kwargs — the prompt's `<Scale>` and structured-output constraint can desync. `GradingRunner` `deepcopy`s each grader per sample; don't defeat this by caching and mutating.
- **`template.format(...)`.** `{query}` = `params["query"]`, `{{`/`}}` are literal braces, `{query.response}` is attribute access (breaks on dicts), `{unknown_key}` raises `KeyError` → wrapped as `GraderError`. Smoke-test every placeholder **and every language branch**.
- **Bilingual template.** `PromptTemplate.messages = {EN: [...], ZH: [...]}`; `self.language` picks one via (constructor arg → `LANGUAGE` env var → `EN`). Deploying with `LANGUAGE=zh` in the env picks ZH prompts — if the ZH branch is a stub, prompts break. Provide both or pin `language=LanguageEnum.EN` at construction.
- **`to_dict` / `from_config` are lossy.** `LLMGrader.to_dict()` drops `self.model` unless you passed a dict; strategy and callback are not recorded; `AgenticGrader.from_config` hardcodes `ReActAgent`. Treat config as a starting point; persist construction code + git SHA.

### Trap 5 — `callback` is the silent mutator

`LLMGrader(..., callback=fn)` receives the `ChatResponse` and can mutate `.parsed` before the grader extracts `score`. This is how `TrajectoryComprehensiveGrader` converts per-step 1-5 outputs into a [0, 1] overall score — the prompt asks for 1-5, the visible `GraderScore.score` is [0, 1]. If a grader's documented output doesn't match its prompt scale, **look for a callback**. For custom graders, a callback is the right place to clamp, aggregate multi-field JSON into a scalar, or add computed metadata.

## Evaluation strategies — orthogonal to grader type

`BaseGrader.__init__(strategy=...)` wraps `_aevaluate` to improve reliability.

| Strategy | Behavior | When |
|---|---|---|
| `DirectEvaluationStrategy` (or `None`) | Call once, return | Cost-constrained, deterministic graders, prototyping |
| `VotingEvaluationStrategy(num_votes=N, tie_breaker=...)` | N calls, return modal score | Discrete/ordinal scores (1-5, binary). Prefer odd N. Tie-breakers: `MIN` (pessimistic — safety-critical), `MAX`, `CLOSEST_TO_MEAN`. |
| `AverageEvaluationStrategy(num=N)` | N calls, mean numeric fields | Continuous scores, smoothing LLM noise |
| `GRPOTournamentEvaluationStrategy` | Pairwise tournament | RL / GRPO training only |

N=5 → 5x LLM cost per sample. N=3 typically buys ~70% of the noise reduction at 3x cost.

## Pointwise vs Listwise — a decision, not a setting

- **POINTWISE.** "Given one response, how good is it?" Absolute score. Cheap, but prone to calibration drift across queries.
- **LISTWISE.** "Given K responses, order them." A `GraderRank` output. More robust for relative comparisons. Only valid when ≥2 responses share a query. `RankValidation` (`openjudge/graders/schema.py:123`) enforces the rank is a permutation of `1..n`.

Rules of thumb:

- Absolute performance tracking → POINTWISE.
- A/B model comparison, human-preference benchmarking → LISTWISE.
- RLHF preference data → LISTWISE (2-item pairwise is the special case).

### Pairwise A/B checklist (`ComprehensivePairwiseGrader` or listwise Auto-Rubric)

Any pairwise A/B answer must address these six points:

1. **Calibration-drift rationale** — why LISTWISE beats pointwise (pointwise anchors drift across queries; relative order within a query is stable).
2. **`VotingEvaluationStrategy(num_votes=N)` with odd `N>=3`.** Majority vote collapses per-call noise; odd avoids ties at the strategy layer.
3. **Win-rate with confidence interval**, not mean — Wilson or binomial CI for `k wins / n trials` at 95%. Sample code: `wins = sum(r.rank[0] == 1 for r in col)`.
4. **Position-bias randomization.** Randomize `[responseA, responseB]` order per query; otherwise the judge systematically favors position 1 or 2. Record the per-query order so you can de-bias post-hoc.
5. **Template/dimension-fit caveat.** `ComprehensivePairwiseGrader` is **skill-templated** — it uses AI-Skill dimensions (relevance, completeness, safety, structure) with fixed `DEFAULT_DIMENSION_WEIGHTS`. For RAG / code / domain-specific A/B, override `dimension_weights=` or write a custom listwise grader; don't inherit the AI-Skill defaults silently.
6. **Mixing-with-pointwise warning.** `WeightedSumAggregator` silently ignores `GraderRank`. Never put a pairwise grader and a pointwise grader into the same weighted-sum column — the rank is dropped.

Tie-breaker menu for `VotingEvaluationStrategy`: `MIN` (pessimistic — use when false-positives are costly, e.g. safety), `MAX` (optimistic), `CLOSEST_TO_MEAN` (smoothing).

## Anti-patterns

1. `LLMGrader` where `FunctionGrader` suffices (e.g., LLM-judge for string equality).
2. `RelevanceGrader` used as a correctness proxy — it measures "addresses the query", not "is true".
3. Mixing `TrajectoryAccuracyGrader` (1-3) and `TrajectoryComprehensiveGrader` ([0,1]) in one `WeightedSumAggregator` without normalization. They look like siblings, aren't.
4. Pointwise `ComprehensiveGrader` to compare model A vs B on **different queries** — pointwise scores aren't comparable across queries; use LISTWISE within a query.
5. `AgenticGrader.from_config` with an external-framework agent — hardcodes `ReActAgent`. Use the ctor.
6. `structured_model=None` + assuming range enforcement — defaults are permissive.
7. Trusting mean composite scores before `DistributionAnalyzer` per column.

## Post-run audit checklist

Every run, every time:

1. `len([r for r in col if isinstance(r, GraderError)])` per grader — count errors; investigate if any column > ~2%.
2. `DistributionAnalyzer` per column — min/max match documented scale; `std > 0` (zero = prompt/schema broken).
3. Log-grep triad:
   ```bash
   grep "recovered via embedded-JSON fallback" logs/*.log | wc -l
   grep "recovered via regex fallback"          logs/*.log | wc -l
   grep "Automatically switching to 'json_object'" logs/*.log | wc -l
   ```
   First two = prompt quality drift; third = silent schema downgrade. Alert if any rate > 1%.
4. If composite uses `WeightedSumAggregator`: verify every input column has the same scale, or you normalized.

## Additional Resources

- `openjudge-rubric-workflow` — for custom graders (manual or generated from data/task-description) and for the full runner/aggregator/analyzer lifecycle.
- `openjudge-agent-eval` — for agent evaluation (granularity × cognitive module) and agent-specific data shapes.
- `openjudge-index` — for cross-skill routing and the rename-and-refactor debiasing table.
