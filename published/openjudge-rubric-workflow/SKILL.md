---
name: openjudge-rubric-workflow
description: >-
  Use this skill when no built-in OpenJudge grader fits, or when wiring up
  an evaluation pipeline at scale. Triggers include "generate a grader /
  rubric", "Auto-Rubric", "SimpleRubricsGenerator",
  "IterativeRubricsGenerator", "custom LLMGrader subclass", "GradingRunner",
  "max_concurrency", "aggregator error handling", "DistributionAnalyzer",
  "CorrelationAnalyzer", and pipeline debugging ("why is my composite
  weird", "near-zero correlation on generated grader"). Covers the three
  rubric-generation paths (manual, zero-shot, data-driven Auto-Rubric), the
  silent empty-rubrics failure IterativeRubricsGenerator can produce without
  raising, the runner/aggregator/analyzer lifecycle with error-exclusion
  and mapper traps, post-run audit discipline, and reproducibility (configs
  are lossy). Use after openjudge-grader-selection confirms no built-in
  fits. Do NOT trigger for picking built-in graders
  (use openjudge-grader-selection) or agent granularity
  (use openjudge-agent-eval).
version: "1.0.0"
license: MIT
---

# Rubric Generation + Pipeline Lifecycle

When a built-in grader doesn't fit, you build one. When you have more than a
handful of samples, you run them through `GradingRunner`. Both have
under-documented failure modes that produce plausible-looking garbage without
raising. This skill is about recognizing and avoiding them.

Two core reflexes:

1. **Never trust a generated rubric without inspecting it.** `IterativeRubricsGenerator` can return an `LLMGrader` with an empty `kwargs["rubrics"]` string — no exception, no hard warning at the API level (`openjudge/generator/iterative_rubric/generator.py:414-438` silently discards invalid rubrics). An LLM judging with no criteria still produces plausible numbers.
2. **`GradingRunner.arun` never raises.** Every failure becomes a `GraderError` in the result column. `WeightedSumAggregator` silently excludes errors from the denominator (`openjudge/runner/aggregator/weighted_sum_aggregator.py:62-82`), which shifts per-sample composites depending on which graders survived. Audit every run.

**Name-check discipline.** Before shipping any import or class reference in docs, verify it exists in `openjudge/graders/` or `openjudge/generator/`. Plausible-looking names (e.g. "CorrelationGrader") can slip into designs and break at runtime. The real classes: `CorrelationAnalyzer` (analyzer, not grader), `CorrectnessGrader`, `RelevanceGrader`, `ComprehensivePairwiseGrader` — the tree has strict naming.

## Path 1 — Generate a grader from a task description (`SimpleRubricsGenerator`)

**When:** You have domain knowledge but no labeled data, and you need a grader today.

```python
from openjudge.generator.simple_rubric import (
    SimpleRubricsGenerator, SimpleRubricsGeneratorConfig,
)

config = SimpleRubricsGeneratorConfig(
    grader_name="translation_quality",
    model=model,
    task_description="English→Chinese translation for technical docs",
    scenario="Users need accurate, fluent translations",
    min_score=1, max_score=5,
)
grader = await SimpleRubricsGenerator(config).generate(dataset=[], sample_queries=[...])
```

**How it works:** one LLM call proposes rubrics in Theme-Tips structure, rubrics
are injected into a default POINTWISE/LISTWISE template, the whole thing is
wrapped in an `LLMGrader`.

**Quality levers:**

- **Specific `task_description`**: "chatbot" is useless. "Medical triage chatbot for emergency room nurses, prioritizing safety over completeness" is great.
- **`sample_queries`**: even fake ones help the LLM calibrate what input looks like.
- **`default_rubrics`**: used if generation fails (JSON parse / network). Always set a reasonable fallback.

**Failure modes:** over-general themes ("Accuracy", "Helpfulness" for every task), or rubrics that cover style but not the dimension you actually care about. Mitigate by sharpening `task_description`.

`SimpleRubricsGenerator` is a **Day 1** tool, not a destination. If you're shipping to production, collect labels and move to Path 2.

## Path 2 — Generate a grader from labeled data (`IterativeRubricsGenerator`)

**When:** You have 50-100+ labeled preference pairs or scored responses.

This is the flagship Auto-Rubric path (paper: arxiv 2510.17314 — training-free, claims ~70 pairs is enough for a small model to match a trained judge).

### The two-stage pipeline

**Stage 1 — Query-specific rubric generation.** Per labeled example, run a `Propose → Evaluate → Revise` loop:

1. Propose query-specific rubrics.
2. Apply them, predict a score/rank.
3. Compare to ground-truth label.
4. If mismatch: generate feedback → revise → up to `max_epochs` (default 5).
5. Emit rubrics + `rubric_valid: True | False` flag.

**Stage 2 — Aggregation + optional categorization.**

- `ALL_SAMPLES` mode (≤100 examples; auto-selected by size): concatenate all *validated* rubrics.
- `SMART_SAMPLING` mode (>100 examples): MCR²-based diverse non-redundant subset.
- `enable_categorization=True`: LLM-cluster rubrics into `categories_number` themes for readability.

Output: a consolidated rubric string in `grader.kwargs["rubrics"]`.

### The silent empty-rubrics failure mode — **always validate**

In `openjudge/generator/iterative_rubric/generator.py` around lines 414-438:

```python
for idx, result in enumerate(all_results):
    rubric_valid = result.get("rubric_valid", False)
    rubrics = result.get("rubrics", [])
    if rubrics and rubric_valid:
        all_rubrics.extend(rubrics)   # only validated rubrics kept
    elif rubrics and not rubric_valid:
        logger.warning(...); failed_count += 1   # INVALID rubrics silently discarded
    else:
        logger.warning(...); failed_count += 1   # nothing generated
```

If every training example fails validation (noisy labels, weak judge, too-few
examples), `all_rubrics` is empty. The generator still returns an `LLMGrader`.
Its `kwargs["rubrics"]` is an empty string. The prompt's `{rubrics}` section
renders as nothing. The LLM judges **with no criteria** — it produces plausible
numbers from prior.

### Mandatory post-generation validation

Paste this verbatim — the explicit thresholds are the load-bearing part:

```python
grader = await IterativeRubricsGenerator(config).generate(labeled)

# 1. Rubrics non-empty — >100 chars is healthy; <50 is definitely broken
rubrics = grader.kwargs.get("rubrics", "")
assert rubrics and len(rubrics) > 100, f"Generation failed silently: {rubrics!r}"

# 2. Template actually references rubrics
tpl = str(grader.get_template())
assert "{rubrics}" in tpl or "rubrics" in tpl.lower(), "Rubrics not wired into template"

# 3. Eyeball the themes — generic names ("Accuracy", "Relevance") mean the
# model didn't use your data. Domain-specific themes are the good signal.
print("Generated rubrics:\n", rubrics)

# 4. Held-out correlation check — a rubricless grader shows near-chance correlation
from openjudge.analyzer.validation import CorrelationAnalyzer
holdout = labeled[:10]
runner = GradingRunner(grader_configs={"mine": grader}, max_concurrency=4)
results = await runner.arun(holdout)
corr = CorrelationAnalyzer().analyze(holdout, results["mine"])
assert corr.pearson > 0.4, f"Generated grader didn't learn: r={corr.pearson:.2f}"
```

Skip any of these four checks and you can ship a silently-broken grader. Reserve the holdout slice **up front** (before generation), not after — otherwise correlation is measured on in-sample data.

### Data format

Pointwise (`IterativePointwiseRubricsGeneratorConfig`):

```python
{"query": "...", "response": "...", "label_score": 5}   # int in [min_score, max_score]
```

Listwise / Pairwise (`IterativeListwiseRubricsGeneratorConfig`):

```python
{"query": "...", "responses": ["A", "B", "C"], "label_rank": [2, 1, 3]}
# label_rank must be a valid permutation of 1..N. RankValidation enforces
# this at GraderRank creation; during rubric generation the validator may be
# bypassed, producing confusing errors. Sanitize upstream.
```

### Parameter tuning

| Goal | `query_specific_generate_number` | `enable_categorization` | `categories_number` |
|---|---|---|---|
| Fast prototype | 1 | False | — |
| Small dataset (50-100) | 1 | False | — |
| Medium dataset (≤100) | 2-3 | True | 5 |
| Large dataset (>100, smart sampling) | 1 | True | 5 |

`batch_size=10`, `mcr_batch_size=10`: defaults fine. `max_epochs=5`: raise to 8-10 if you see many "failed validation" warnings before blaming the data. `min_increment_threshold=0.002`, `patience=2`: for SMART_SAMPLING; raise `patience` to 4+ on rich datasets.

### Debugging checklist (in order)

1. Are rubrics non-empty (`len(grader.kwargs["rubrics"]) > 50`)?
2. Are rubrics referenced in the template (grep for `{rubrics}`)?
3. What fraction of training examples validated successfully? (log shows `successful_count vs failed_count`)
4. Does the grader beat a trivial baseline on held-out labels?
5. Are themes generic ("Accuracy", "Relevance") or domain-specific? Generic = model ignored your data, usually from noisy labels or a vague `task_description`.

## Path 3 — Write a custom `LLMGrader` subclass

**When:** Rubrics aren't the right abstraction, or you need expert-authored criteria.

```python
class MyGrader(LLMGrader):
    DEFAULT_TEMPLATE = PromptTemplate(messages={
        LanguageEnum.EN: [
            ChatMessage(role="system", content=LLMGrader.SYSTEM_PROMPT_EN),
            ChatMessage(role="user", content=MY_EN_PROMPT),
        ],
        LanguageEnum.ZH: [...],
    })

    def __init__(self, model, **kwargs):
        super().__init__(
            model=model, name="my_grader",
            mode=GraderMode.POINTWISE,
            template=self.DEFAULT_TEMPLATE,
            **kwargs,
        )
```

The prompt should include:

- `<Constraints>` — what's in/out of scope.
- `<Scale>` — explicit meanings for each score level (match `min_score/max_score`).
- `<Output Schema>` — JSON shape example.
- All `{placeholder}` names you plan to pass via `aevaluate`.

See `openjudge-grader-selection` for the `LLMGrader` internal traps (silent
schema downgrade, three-tier fallback chain, callback, bilingual template).
Custom graders need to reason about all of them.

## Rubric mechanics — what "rubric" means

A rubric is a structured evaluation criterion injected into the judge prompt. Both generators produce Theme-Tips structure:

```
Rubric 1:
Theme: Accuracy
- Tip1: The response should contain factually correct information
- Tip2: Claims should be verifiable and not contradict established knowledge

Rubric 2:
Theme: Completeness
...
```

Rubrics live on the grader as `grader.kwargs["rubrics"]` (a string). They are rendered into the template via Python string formatting (the `{rubrics}` placeholder). If the final template doesn't reference `{rubrics}`, the generated rubrics are silently unused — **inspect `grader.get_template()` to verify the hookup**.

Rubric language must match prompt language. Re-running the generator on the same data produces different rubrics (LLM stochasticity); persist `grader.to_dict()` — but be aware `to_dict()` drops the `model` object unless you originally passed a dict. Commit the construction code as well.

## Pipeline lifecycle — Runner → Aggregator → Analyzer

### Stage 1 — `GradingRunner`

**Config construction — four equivalent shapes.** Pick one style and stay consistent:

```python
{"relevance": GraderConfig(grader=g, mapper={...})}         # explicit
{"relevance": g}                                            # bare grader
{"relevance": (g, {"q": "query", "a": "answer"})}           # tuple
{"relevance": {"grader": g, "mapper": {"q": "query"}}}      # dict (idiomatic)
```

**What `arun(dataset)` actually does:**

1. Builds a **cartesian product** of (grader × sample) coroutines. N graders × M samples = N*M coroutines.
2. All submit to one `SemaphoreResourceExecutor(max_concurrency)` — the limit is **global** across graders and samples.
3. `asyncio.gather` awaits all; results return in submission order.
4. Organizes results into `{grader_name: [result_for_sample_0, ...]}`.
5. Per configured aggregator, iterates samples and calls `aggregator({grader_name: result}, ...)` per sample, appending to `results[aggregator.__name__]`.

**Consequences that matter:**

- **`max_concurrency` is global.** 5 graders on 100 samples with `max_concurrency=10` means at most 10 LLM calls in flight total — **not** 10 per grader. A 10-grader pipeline usually needs a higher ceiling than a single-grader one.
- **Errors never propagate.** `arun` wraps every exception as `GraderError`. Your call never raises. You must inspect for errors yourself.
- **The mapper runs once per (grader × sample).** Heavy mappers (e.g., re-encoding images) multiply accordingly — pre-map the dataset before `arun` if this is expensive.
- **Each grader is `deepcopy`d per sample** to isolate stateful graders with an `EvaluationStrategy`. Don't cache state on `self` expecting it to persist.

### Mappers — how your data talks to the grader

Dict mapper:

```python
mapper = {"query": "user_prompt", "response": "model_output"}
# kwargs["query"] = data["user_prompt"], kwargs["response"] = data["model_output"]
```

Callable mapper:

```python
def mapper(sample: dict) -> dict:
    return {"query": sample["q"], "response": sample["a"],
            "context": "\n".join(sample["docs"])}
```

**No mapper:** the full sample is splatted into `aevaluate(**sample)`. Missing required keys → `KeyError` → `GraderError`.

**Direction trap:** the dict is `{grader_side_key: data_side_key}`. Docs examples sometimes get it backwards. Verify against the grader's docstring or `_aevaluate` signature.

### Multiple datasets — `arun_multiple_datasets`

Evaluates sequentially, sharing the concurrency budget. Disables per-dataset progress bars to avoid tqdm conflicts.

### `show_progress=True` is default

Turn it off in CI / non-interactive contexts; tqdm contaminates logs otherwise.

### Stage 2 — Aggregators

Three production aggregators:

- `WeightedSumAggregator(name, weights)` — weighted mean; equal weights if `weights=None`.
- `MaxAggregator(name)` — max across graders.
- `MinAggregator(name)` — min across graders.

All return a `GraderScore` named `aggregator.__name__`, which becomes a column in the `RunnerResult`.

**Two silent failure modes**, both in `openjudge/runner/aggregator/weighted_sum_aggregator.py:62-82`:

**Mixed-scale composites.** The aggregator multiplies raw `score` by weight and sums. No scale check, no expected-range metadata, no warning. Worked example — four graders at equal weight 0.25, raw `correctness=4`, `tool_sel=5`, `trajectory=0.7`, `plan=1.0`:

```
composite = 0.25*4 + 0.25*5 + 0.25*0.7 + 0.25*1.0 = 2.675
```

The 1-5 graders contribute ~2.25 (84%); the binary + [0,1] graders contribute 0.425. Adjusting the binary/continuous weights barely moves 2.675 — users misattribute to "grader noise". **Fix: normalize every input to [0, 1] first.** Formulas (say the denominator out loud — `(x-1)/4` for 1-5, `(x-1)/2` for 1-3, not `/5` or `/3`).

**`GraderError` silently leaves the denominator.** The `if isinstance(result, GraderScore)` guard at line 66 excludes errors from both `weighted_sum` and `total_weight`. The final composite is `weighted_sum / total_weight` over the **surviving** graders — different samples can have different surviving sets. Cross-sample composites become non-comparable, and the aggregator does not record which graders were skipped per sample. **Default policy: write a custom aggregator that returns `GraderError` if any input errored, or substitute a worst-case/neutral value.**

### Canonical normalization wrapper — paste template

```python
from openjudge.graders.function_grader import FunctionGrader
from openjudge.graders.schema import GraderMode, GraderScore, GraderError

def make_normalizer(inner, lo, hi, new_name):
    """Wrap a 1-5 or 1-3 LLM grader so WeightedSumAggregator gets [0, 1]."""
    async def norm(**kwargs):
        r = await inner.aevaluate(**kwargs)
        if isinstance(r, GraderError):
            return r
        clamped = max(lo, min(hi, float(r.score)))   # ALWAYS clamp before dividing
        return GraderScore(
            name=new_name,
            score=(clamped - lo) / (hi - lo),
            reason=r.reason,
            metadata={**r.metadata, "original_score": r.score},   # preserve pre-norm value
        )
    return FunctionGrader(func=norm, name=new_name, mode=GraderMode.POINTWISE)
```

`original_score` in metadata preserves the pre-normalization value for downstream analyzers / debugging.

### Multiple aggregators per run

```python
aggregators=[
    WeightedSumAggregator(name="overall"),
    MaxAggregator(name="worst_case"),   # e.g., highest safety concern
]
```

Each produces its own column. Useful when you want balanced score **and** worst-dimension side-by-side.

### Writing a custom aggregator

Subclass `BaseAggregator`, implement `__call__(grader_results, **kwargs) -> GraderResult`. Payoff patterns: error-aware (return `GraderError` or `metadata["missing_graders"]`), scale-aware (`scales: Dict[str, Tuple[float, float]]` normalize inline), typed combinations (`WeightedSumAggregator` silently ignores `GraderRank` — usually wrong when you want to mix pointwise + listwise).

### Stage 3 — Analyzers (post-hoc, **synchronous**)

Don't wrap in `asyncio.run` — analyzer `analyze()` methods are sync.

**Statistical** (`openjudge.analyzer.statistical`):

- `DistributionAnalyzer` — mean, std, min, max, percentiles, histogram. The tool for sanity-checking a grader's score distribution against its documented range.
- `ConsistencyAnalyzer` — variance across multiple runs of the same dataset (characterizes LLM judge noise).

**Validation** (`openjudge.analyzer.validation`):

- `AccuracyAnalyzer` — exact-match accuracy vs labels.
- `PrecisionAnalyzer`, `RecallAnalyzer`, `F1ScoreAnalyzer` — classification metrics.
- `FalsePositiveAnalyzer`, `FalseNegativeAnalyzer` — surface specific disagreement samples. Primary tool during grader debugging.
- `CorrelationAnalyzer` — Pearson/Spearman between scores and labels. The right metric for continuous labels.

Typical use: "Is my generated grader picking up signal?" → run it on a held-out labeled slice, feed `(dataset, results["my_grader"])` to `CorrelationAnalyzer`. Near-zero correlation traces back to silent failures — empty rubrics, schema downgrade, or a broken mapper.

### Concurrency control

- `SemaphoreResourceExecutor(max_concurrency)` is the default; caps in-flight async tasks.
- Most LLM providers will 429 before the code default of 32. Start at 4-8.
- For slow judges with strict TPM limits: low `max_concurrency` + `AverageEvaluationStrategy` for noise smoothing beats high parallelism.

## Operational checklist for a new pipeline

```
□ Pick graders (openjudge-grader-selection decision tree)
□ For each grader: verify score range against the scale map
□ Decide per-grader-reporting vs composite
   □ If composite: choose normalization strategy BEFORE coding the aggregator
   □ If cost-sensitive: compute judge-call budget explicitly
     (N_llm_graders × N_samples × strategy_multiplier — e.g., 2 LLM
      graders × 2000 samples × 1 vote = 4000 judge calls)
□ Build mapper (dict or callable) for each grader;
  verify field names match the grader's expected kwargs
□ Set max_concurrency BELOW provider rate limit (start 4-8).
  Remember it's a SINGLE semaphore, global across graders × samples.
□ show_progress=False in CI / non-interactive contexts (tqdm contaminates logs)
□ Smoke-test on 10-50 samples. Inspect:
   □ GraderErrors present? Why?
   □ GraderScore.score values within expected range?
   □ std > 0.05 per column (zero = prompt/schema broken, not "clean data")
   □ metadata contains expected sub-fields for complex graders?
   □ Manual spot-check 3-5 samples end-to-end
□ Scale up to full dataset
□ Post-run: DistributionAnalyzer per column — catch scale surprises
□ Post-run: log-grep audit
   grep "recovered via embedded-JSON fallback" logs/*.log | wc -l
   grep "recovered via regex fallback"          logs/*.log | wc -l
   grep "Automatically switching to 'json_object'" logs/*.log | wc -l
□ Labels available? AccuracyAnalyzer / CorrelationAnalyzer — confirm signal
□ Persist construction code + git SHA (to_dict drops model, strategy, callback)
```

## Anti-patterns

1. Treating `arun` as fire-and-forget because it doesn't raise. Errors are wrapped.
2. `show_progress=True` in CI.
3. Setting `max_concurrency` per grader intuition — it's a single global semaphore.
4. Aggregating without scale normalization — the compounding silent failure.
5. `DistributionAnalyzer` on `GraderRank` columns — it expects scalars.
6. Relying on `grader.to_dict()` alone for reproducibility.
7. Skipping smoke-test phase; silent failures compound across thousands of samples.
8. Using `IterativeRubricsGenerator` output without validating rubrics are non-empty.

## Cross-skill pointers

- `openjudge-grader-selection` — the decision tree for picking a base class and a concrete grader, the full score-scale map, `LLMGrader` internal traps (structured_model downgrade, fallback chain, callback), evaluation strategies.
- `openjudge-agent-eval` — granularity and cognitive-module axes for agent evaluation, agent-specific data shapes, `AgenticGrader` identity.
- `openjudge-index` — rename/refactor debiasing, cross-skill routing.
