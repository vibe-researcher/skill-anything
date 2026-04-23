# Evaluation Recipes — Scenario Cheatsheet

Copy-paste starting points for the eight most common OpenJudge evaluation
scenarios. Each recipe bakes in the scale-safety, error-handling, and
validation discipline that the main skills explain the reasoning for. Adapt,
do not reproduce blindly.

## Table of contents

1. Single-response QA correctness against references
2. Multi-dimensional application eval (same-scale composite)
3. Agent end-to-end with mixed scales (normalized to [0, 1])
4. Model A vs Model B pairwise comparison
5. Grader-from-labeled-data with sanity check
6. Reliability-critical eval (voting strategy)
7. Agentic judge (tool-augmented, citation verification)
8. Distribution sanity check — catch silent scale bugs early

---

## Recipe 1 — Single-response QA correctness (with references)

**Scenario:** QA dataset with reference answers; rate each response.
**Decisions:** Final-Response granularity, 1-5 `CorrectnessGrader`, no aggregation.

```python
import asyncio
from openjudge.models import OpenAIChatModel
from openjudge.graders.common import CorrectnessGrader
from openjudge.runner import GradingRunner
from openjudge.analyzer.statistical import DistributionAnalyzer

async def main():
    model = OpenAIChatModel(model="qwen3-max")
    dataset = [
        {"q": "Capital of France?", "a": "Paris", "ref": "Paris"},
        {"q": "2+2?", "a": "4", "ref": "4"},
    ]
    grader_configs = {
        "correctness": {
            "grader": CorrectnessGrader(model=model),
            "mapper": {"query": "q", "response": "a", "reference_response": "ref"},
        },
    }
    runner = GradingRunner(grader_configs=grader_configs, max_concurrency=4)
    results = await runner.arun(dataset)

    errors = [r for r in results["correctness"] if not hasattr(r, "score")]
    if errors:
        print(f"WARNING: {len(errors)} parse errors")

    stats = DistributionAnalyzer().analyze(dataset, results["correctness"])
    print(f"Mean: {stats.mean:.2f} / 5, Std: {stats.std:.2f}")

asyncio.run(main())
```

Interpretation: mean < 3 on a 1-5 scale means the model fails more than it
succeeds. Use `FalseNegativeAnalyzer` to surface specific misses if you have
binary labels.

---

## Recipe 2 — Multi-dimensional application eval

**Scenario:** E-commerce chatbot; relevance + hallucination + tool-selection.
**Decision:** All three are 1-5 — safe to weighted-sum **because scales match**.

```python
from openjudge.graders.common import RelevanceGrader, HallucinationGrader
from openjudge.graders.agent.tool.tool_selection import ToolSelectionGrader
from openjudge.runner.aggregator import WeightedSumAggregator

grader_configs = {
    "relevance":      {"grader": RelevanceGrader(model=model),
                       "mapper": {"query": "query", "response": "response"}},
    "hallucination":  {"grader": HallucinationGrader(model=model),
                       "mapper": {"query": "query", "response": "response", "context": "context"}},
    "tool_selection": {"grader": ToolSelectionGrader(model=model),
                       "mapper": {"query": "query", "tool_definitions": "tool_definitions",
                                  "tool_calls": "tool_calls"}},
}
aggregator = WeightedSumAggregator(
    name="overall",
    weights={"relevance": 0.3, "hallucination": 0.4, "tool_selection": 0.3},
)
runner = GradingRunner(grader_configs=grader_configs, aggregators=[aggregator], max_concurrency=4)
results = await runner.arun(dataset)

# Smoke-test for silent scale drift: the day someone adds a 0-1 grader here,
# this assertion catches it.
assert all(1 <= r.score <= 5 for r in results["overall"] if hasattr(r, "score")), "Scale drift!"
```

---

## Recipe 3 — Agent end-to-end with mixed scales

**Scenario:** Correctness (1-5) + tool-selection (1-5) + trajectory-comprehensive (0-1) + plan-feasibility (binary).
**Decision:** Mixed scales. Normalize each to [0, 1] before aggregating.

```python
from openjudge.graders.function_grader import FunctionGrader
from openjudge.graders.schema import GraderMode, GraderScore, GraderError

def make_normalizer(inner_grader, lo: float, hi: float, new_name: str):
    async def normalize(**kwargs):
        r = await inner_grader.aevaluate(**kwargs)
        if isinstance(r, GraderError):
            return r
        clamped = max(lo, min(hi, float(r.score)))  # clamp BEFORE normalizing
        return GraderScore(
            name=new_name, score=(clamped - lo) / (hi - lo),
            reason=r.reason,
            metadata={**r.metadata, "original_score": r.score, "scale": (lo, hi)},
        )
    return FunctionGrader(func=normalize, name=new_name, mode=GraderMode.POINTWISE)

norm_correctness = make_normalizer(CorrectnessGrader(model=model),   1, 5, "correctness_norm")
norm_tool_sel    = make_normalizer(ToolSelectionGrader(model=model), 1, 5, "tool_sel_norm")
# trajectory_comprehensive is already [0, 1]; no wrapper needed
# plan_feasibility is binary {0, 1}; no wrapper needed

grader_configs = {
    "correctness":  {"grader": norm_correctness, "mapper": {...}},
    "tool_sel":     {"grader": norm_tool_sel,    "mapper": {...}},
    "trajectory":   {"grader": TrajectoryComprehensiveGrader(model=model),
                     "mapper": {"messages": "msgs"}},
    "plan":         {"grader": PlanFeasibilityGrader(model=model), "mapper": {...}},
}
aggregator = WeightedSumAggregator(
    name="overall",
    weights={"correctness": 0.4, "tool_sel": 0.2, "trajectory": 0.3, "plan": 0.1},
)
runner = GradingRunner(grader_configs=grader_configs, aggregators=[aggregator], max_concurrency=6)
results = await runner.arun(dataset)
```

Without the wrappers, `correctness` contributes up to 0.4 * 5 = 2.0 while
`plan` contributes at most 0.1 * 1 = 0.1 — the 1-5 graders silently dominate.

---

## Recipe 4 — Model A vs Model B pairwise

**Scenario:** A/B compare two model outputs on the same queries.
**Decision:** LISTWISE mode. Summarize by win-rate, not mean score.

```python
from openjudge.graders.skills.comprehensive_pairwise import ComprehensivePairwiseGrader

dataset = [
    {"query": "Explain photosynthesis", "response_a": "...", "response_b": "..."},
]

grader = ComprehensivePairwiseGrader(model=model, mode=GraderMode.LISTWISE)
grader_configs = {"ab_pair": {
    "grader": grader,
    "mapper": lambda s: {"query": s["query"], "responses": [s["response_a"], s["response_b"]]},
}}
runner = GradingRunner(grader_configs=grader_configs, max_concurrency=6)
results = await runner.arun(dataset)

ranks = [r.rank for r in results["ab_pair"] if hasattr(r, "rank")]  # excludes errors
win_rate_a = sum(1 for rk in ranks if rk[0] < rk[1]) / len(ranks) if ranks else 0.0
print(f"A beats B on {win_rate_a:.1%} of samples")
```

`hasattr(r, "rank")` filters out `GraderError`. If you need strict accounting,
count errors separately.

---

## Recipe 5 — Grader from labeled data (Auto-Rubric)

**Scenario:** ~70 labeled pointwise examples → custom grader.
**Decision:** Iterative Rubric path. **Mandatory post-generation sanity check**
to catch the silent empty-rubrics failure mode.

```python
from openjudge.generator.iterative_rubric.generator import (
    IterativeRubricsGenerator,
    IterativePointwiseRubricsGeneratorConfig,
)

labeled = [{"query": "...", "response": "...", "label_score": 5}, ...]  # 50+

config = IterativePointwiseRubricsGeneratorConfig(
    grader_name="my_custom_grader", model=model,
    min_score=1, max_score=5,
    query_specific_generate_number=2,
    enable_categorization=True, categories_number=3,
    task_description="Evaluate... concrete domain-specific description...",
)
grader = await IterativeRubricsGenerator(config).generate(labeled)

# *** MANDATORY SANITY CHECKS ***
rubrics = grader.kwargs.get("rubrics", "")
assert rubrics and len(rubrics) > 100, f"Generation failed silently: {rubrics!r}"
tpl = str(grader.get_template())
assert "rubrics" in tpl.lower(), "Rubrics not wired into template!"
print("Generated rubrics:\n", rubrics)  # eyeball — generic themes = bad

# Validate on held-out slice
from openjudge.analyzer.validation import CorrelationAnalyzer
holdout = labeled[:10]
runner = GradingRunner(grader_configs={"mine": grader}, max_concurrency=4)
results = await runner.arun(holdout)
corr = CorrelationAnalyzer().analyze(holdout, results["mine"])
assert corr.pearson > 0.4, f"Generated grader didn't learn: r={corr.pearson:.2f}"
```

Three distinct silent-failure modes caught before the grader reaches
production: empty rubrics, unwired template, no signal.

---

## Recipe 6 — Reliability-critical eval (voting)

**Scenario:** Safety/harmfulness eval; willing to pay 3-5x to reduce judge noise.
**Decision:** Wrap the grader in `VotingEvaluationStrategy`. Odd `num_votes`.

```python
from openjudge.evaluation_strategy import VotingEvaluationStrategy
from openjudge.graders.common import HarmfulnessGrader

grader = HarmfulnessGrader(
    model=model,
    strategy=VotingEvaluationStrategy(num_votes=5, tie_breaker="min"),  # pessimistic
)
# Each aevaluate now makes 5 LLM calls and returns the modal score.
```

`tie_breaker="min"` is safety-pessimistic for `HarmfulnessGrader` (lower = worse).
Use `"max"` or `"closest_to_mean"` for graders where higher = better.

---

## Recipe 7 — Agentic judge (tool-augmented)

**Scenario:** Verify that citations in a response actually exist / match claims.
The judge needs web search.
**Decision:** `AgenticGrader` + pre-built `ReActAgent`. Budget `max_iterations`.

```python
from openjudge.agentic import ReActAgent
from openjudge.graders.agentic_grader import AgenticGrader

agent = ReActAgent(
    model={"model": "gpt-4o", "api_key": os.environ["OPENAI_API_KEY"]},
    tools=[WebSearchTool()],   # your BaseTool subclasses
    max_iterations=6,
)

template = """
You are verifying references in a scholarly response.
Use search tools to check each reference's existence and correctness.
Output JSON: {{"score": <0.0 or 1.0>, "reason": "<per-reference verdict>"}}
Response to verify: {response}
"""

grader = AgenticGrader(agent=agent, template=template, mode=GraderMode.POINTWISE)

# Low max_concurrency — each sample spawns many LLM + tool calls.
runner = GradingRunner(
    grader_configs={"cite_verify": {"grader": grader, "mapper": {...}}},
    max_concurrency=2,
)
results = await runner.arun(dataset)
```

**Cost warning:** 100 samples * 6 iterations * ~2 LLM calls ≈ 1200+ LLM calls,
plus tool calls. Enable logging.

---

## Recipe 8 — Distribution sanity check

**Scenario:** You built a pipeline; before running on 10k samples, run on 50
and inspect distributions.

```python
from openjudge.analyzer.statistical import DistributionAnalyzer

smoke_data = dataset[:50]
results = await runner.arun(smoke_data)

for col_name, col in results.items():
    scored  = [r for r in col if hasattr(r, "score")]
    errored = [r for r in col if not hasattr(r, "score")]
    stats = DistributionAnalyzer().analyze(smoke_data, scored)
    print(f"{col_name:20s} n={len(scored):3d} errs={len(errored):3d} "
          f"min={stats.min:.2f} mean={stats.mean:.2f} max={stats.max:.2f} std={stats.std:.2f}")
```

What to check:

- `min`/`max` within the expected range for that grader (see scale reference in `openjudge-grader-selection`).
- `errs` count low — big = parse failures; investigate before scaling up.
- `std > 0` — zero std means the grader is stuck on one value (prompt or schema broken).
- `mean` matches your expectation — wildly off means wrong mapper or mis-documented scale.
