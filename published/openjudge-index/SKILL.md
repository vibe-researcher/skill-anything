---
name: openjudge-index
description: >-
  Use this skill whenever a task mentions OpenJudge, py-openjudge, LLM-as-judge
  evaluation, LLM grader, reward model, rubric generation, trajectory
  evaluation, or tool-call grading. Routes to three sister skills
  (openjudge-grader-selection, openjudge-rubric-workflow, openjudge-agent-eval)
  and de-biases outdated priors: the library was renamed from rm-gallery to
  py-openjudge (v0.2.0 breaks v0.1.x), "Reward" became "Grader", AgenticGrader
  no longer accepts model=/tools= (must pre-build an agent), and external-framework
  adapters live in cookbooks/ not openjudge.agentic.adapters. Also use this when
  unsure which OpenJudge skill applies, or when triage / orientation is needed
  before writing code. Do NOT trigger for OpenAI Evals, DeepEval, TruLens,
  lm-evaluation-harness, or non-evaluation LLM work.
version: "1.0.0"
license: MIT
---

# OpenJudge Orientation & Skill Routing

OpenJudge (package: `py-openjudge`, import: `openjudge`) is an async evaluation
framework that turns an (input, output) sample into a scalar `GraderScore` or a
`GraderRank`. The same `Grader` abstraction is reused as a **reward model** in
RLHF/GRPO pipelines — "judge" and "reward" are synonyms.

Before writing any code: read the debiasing table, then pick a sister skill.

## Debiasing: what your prior probably has wrong

| Outdated prior (pre-2026) | Current truth (v0.2.0+, 2026) |
|---|---|
| `pip install rm-gallery`, `from rm_gallery import ...` | `pip install py-openjudge`, `from openjudge import ...`. v0.1.x lives on branch `v0.1.7-legacy`; the API is **not backward compatible**. |
| "Reward" is the primary concept | **"Grader"** is primary. Don't search for `RewardModel`; look for `BaseGrader` / `LLMGrader` / `FunctionGrader` / `AgenticGrader`. |
| `GraderScore.score ∈ [0, 1]` | **FALSE.** `score: float` has no validator. Different graders use different scales (1-5, 1-3, binary, 0-1). |
| `AgenticGrader(model=..., tools=[...])` | Wrong since v0.2.0 "unified interface" refactor. Accepts only a pre-built `agent: BaseAgent`. Build `ReActAgent` (or a LangChain/AgentScope adapter) first, then pass it in. |
| `from openjudge.agentic.adapters import LangChainAgentAdapter` | Imports nothing usable. The adapter **implementations** live in `cookbooks/agentic_grader/adapters/{langchain,agentscope}.py`. |
| Everything is synchronous | Everything is async. `grader.aevaluate(...)` / `runner.arun(...)`. There is no sync `evaluate`. Sync callers wrap in `asyncio.run`. |
| `from_config` round-trips any grader | Lossy. `LLMGrader.to_dict()` drops the `model` object if you passed an instance; `AgenticGrader.from_config` hardcodes `ReActAgent`; strategy + callback are not serialized. Persist construction code, not just config. |

If your training data suggests something different from the right column, trust the right column.

## Where code lives (mental map)

```
openjudge/
  graders/         # unit of work; ~50 built-ins
    base_grader.py      # BaseGrader (abstract)
    llm_grader.py       # LLMGrader — ~80% of graders subclass this
    function_grader.py  # FunctionGrader — code-based / deterministic
    agentic_grader.py   # AgenticGrader — judge uses its own agent+tools
    schema.py           # GraderMode, GraderScore, GraderRank, GraderError
    common/   agent/   code/   math/   text/   format/
    multi_turn/   multimodal/   skills/
  runner/          # GradingRunner, aggregators, executors
  analyzer/        # statistical + validation (post-hoc, sync)
  generator/       # simple_rubric, iterative_rubric (rubric-as-grader)
  evaluation_strategy/   # Direct / Voting / Average / GRPO
  models/          # OpenAIChatModel, MiniMax, QwenVL
  agentic/         # BaseAgent, BaseTool, ReActAgent
```

Adapters for external agent frameworks (LangChain, AgentScope) are **not** in
the `openjudge` package — they are in `cookbooks/agentic_grader/adapters/`.
Check out the repo or copy the adapter into your project.

## Skill routing

Pick one (or more) based on what you are trying to do.

### `openjudge-grader-selection` — "which grader?"

Use when you have an evaluation goal (correctness, relevance, harmfulness,
tool-call accuracy, code quality, trajectory quality, etc.) and need to pick
the right grader class and concrete grader. Covers:

- The four base classes (`BaseGrader` / `FunctionGrader` / `LLMGrader` / `AgenticGrader`) and when each applies.
- Concrete-grader lookup table by scenario (not by module path).
- Score scales — the single highest-severity landmine. Not all graders return [0, 1].
- `LLMGrader` internal traps (schema downgrades, three-tier fallback chain, callback mechanism) that you need before customizing prompts or debugging parse errors.
- Evaluation strategies: Direct / Voting / Average.

### `openjudge-rubric-workflow` — "I need a grader for a custom task"

Use when no built-in grader matches — either you have a task description or
labeled preference data. Covers:

- Three paths: manual subclass, `SimpleRubricsGenerator` (zero-shot), `IterativeRubricsGenerator` (data-driven Auto-Rubric).
- The silent empty-rubrics failure mode of the iterative path — mandatory post-generation validation.
- `GradingRunner` / aggregator / analyzer lifecycle: how configs wire up, what `max_concurrency` actually controls, how errors leak into composites, and how to audit.
- Scale-aware aggregation and the `WeightedSumAggregator` error-exclusion pitfall.

### `openjudge-agent-eval` — "I'm evaluating an agent, not a chatbot"

Use for agent evaluation — anything with tool calls, multi-step trajectories,
plans, memory, or reflection. Covers:

- Two orthogonal axes: granularity (final / step / trajectory) × cognitive module (action / tool / memory / plan / reflection / observation).
- Data-shape expectations per axis (flat fields vs step-snapshot vs OpenAI messages).
- `AgenticGrader` identity — it is **not** an agent grader; it is a grader that uses an agent. The agent graders live in `openjudge.graders.agent.*` and mostly subclass `LLMGrader`.
- AgentScope / LangChain adapter traps (cookbook path, Toolkit rejection, from_config hardcoding).
- Agent graders are **heterogeneous in scale**: `TrajectoryAccuracyGrader` is 1-3, `TrajectoryComprehensiveGrader` is 0-1, `ToolSelectionGrader` is 1-5, most single-step agent graders are binary. Aggregating them naively is meaningless.

## Reflexes that apply everywhere

These are cross-cutting rules the sister skills reinforce in detail:

1. **Every `GradingRunner` output includes `GraderError` objects — `arun` never raises.** Filter and count them before trusting any result column.
2. **`WeightedSumAggregator` silently drops `GraderError` from the denominator** (`openjudge/runner/aggregator/weighted_sum_aggregator.py:62-82`). Cross-sample composites become non-comparable when different samples have different surviving graders.
3. **Clamp LLM scores client-side.** Pydantic `ge/le` is not enforced server-side for Qwen/Gemini/pai-judge (`openjudge/models/openai_chat_model.py:239-246`); the model can return out-of-range values.
4. **Smoke-test before scaling.** 10-50 samples, `DistributionAnalyzer` per column, verify min/max match documented scale, then scale up.
5. **Log-grep audit after every run:**
   ```bash
   grep "recovered via embedded-JSON fallback"     logs/*.log | wc -l
   grep "recovered via regex fallback"             logs/*.log | wc -l
   grep "Automatically switching to 'json_object'" logs/*.log | wc -l
   ```
   First two = prompt drift; third = silent schema downgrade.
6. **`result.to_dict()` is lossy.** Commit construction code + git SHA, not just YAML.
7. **Name-check every class against the `openjudge` tree.** Plausible-sounding names (e.g. "CorrelationGrader") may not exist — `CorrelationAnalyzer` does, but it's an analyzer. Verify before writing.

## Migration landmines (rm-gallery / pre-v0.2.0 → py-openjudge)

Frequent gotchas for code migrating from legacy versions — each one is a silent foot-gun:

- **Everything is async.** `aevaluate` / `arun` only; there is no sync `evaluate`.
- **`GraderScore.score` has no validator.** Scales vary per grader (1-5, 1-3, binary, [0,1]).
- **`AgenticGrader(model=, tools=)` is dead since v0.2.0.** New signature is `agent=<pre-built BaseAgent>`.
- **Adapters moved.** `cookbooks/agentic_grader/adapters/`, not `openjudge.agentic.adapters`.
- **`to_dict()` is lossy.** Drops model instance, strategy, callback.
- **`RewardModel` → `Grader`.** Subclass `LLMGrader` (subjective) / `FunctionGrader` (deterministic) / `AgenticGrader` (tool-augmented). RLHF preference data → `IterativeListwiseRubricsGeneratorConfig` or `GRPOTournamentEvaluationStrategy`.

## Additional Resources

- [references/evaluation-recipes.md](references/evaluation-recipes.md) — scenario-driven cheatsheet (QA correctness, multi-dim composite, mixed-scale agent eval, A/B comparison, rubric-from-labels, voting, agentic judge, sanity check). Read after routing to a specific sister skill.

## Quick trigger routing

| Trigger phrase in task | Route to |
|---|---|
| "score responses for correctness / relevance / hallucination" | `openjudge-grader-selection` |
| "build a composite score from multiple graders" | `openjudge-grader-selection` + `openjudge-rubric-workflow` |
| "generate a rubric from labeled data / task description" | `openjudge-rubric-workflow` |
| "evaluate a tool-using / ReAct / multi-step agent" | `openjudge-agent-eval` |
| "trajectory grader" / "tool-call accuracy" / "plan feasibility" | `openjudge-agent-eval` |
| "judge that searches the web to verify citations" | `openjudge-agent-eval` (AgenticGrader) + `openjudge-grader-selection` (cost warning) |
| "compare model A vs model B" | `openjudge-grader-selection` (LISTWISE / pairwise) |
| "why is my composite score weird" | `openjudge-rubric-workflow` (aggregator pitfalls) |
| "why does my generated grader produce near-zero correlation" | `openjudge-rubric-workflow` (empty-rubrics failure mode) |
