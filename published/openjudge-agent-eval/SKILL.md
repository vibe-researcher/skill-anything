---
name: openjudge-agent-eval
description: >-
  Use this skill when evaluating an LLM agent — anything with tool calls,
  multi-step trajectories, plans, memory, or reflection. Triggers include
  "agent evaluation", "tool-call grader", "trajectory grader",
  "TrajectoryAccuracyGrader", "TrajectoryComprehensiveGrader",
  "AgenticGrader", "LangChain adapter", "AgentScope adapter",
  "PlanFeasibilityGrader", "action loop detection", or questions about
  granularity (final vs step vs trajectory) and cognitive module (action,
  tool, memory, plan, reflection, observation). Covers the two-axis decision
  matrix, per-granularity data-shape requirements, disambiguates AgenticGrader
  (grader that uses an agent) from openjudge.graders.agent.*, LangChain and
  AgentScope adapter traps (live in cookbooks/, not openjudge.agentic.adapters),
  and heterogeneous-scale aggregation. Do NOT trigger for chatbot/QA scoring
  without tool use (use openjudge-grader-selection) or rubric generation
  (use openjudge-rubric-workflow).
version: "1.0.0"
license: MIT
---

# Agent Evaluation

Evaluating an LLM agent is **not** evaluating a chatbot. Scoring an agent with
only a `CorrectnessGrader` silently discards most of the signal about *why*
the agent fails. This skill establishes the mental model OpenJudge assumes, so
you can pick graders that give diagnostic, not just summative, signal.

Core reflexes:

1. **Pick from both axes.** Granularity (final / step / trajectory) **and** cognitive module (action / tool / memory / plan / reflection / observation) are orthogonal. A good pipeline spans both.
2. **Agent graders are heterogeneous in scale.** `TrajectoryAccuracyGrader` is 1-3, `TrajectoryComprehensiveGrader` is 0-1, `ToolSelectionGrader` is 1-5, most single-step agent graders are binary. Aggregating them naively produces numbers with no unit.
3. **`AgenticGrader` is not an agent grader.** It is a grader that uses an agent to evaluate. The graders *of* agents live in `openjudge.graders.agent.*` and mostly subclass `LLMGrader`. Since v0.2.0 it accepts `agent=` only (not `model=/tools=`); adapters live in `cookbooks/agentic_grader/adapters/`, **not** `openjudge.agentic.adapters`.

## Two orthogonal axes

### Axis 1 — Granularity: what window are you scoring?

| Granularity | What it measures | Paired with |
|---|---|---|
| **Final Response** | Task success from user's POV — did the agent give the right answer? | Common graders (`CorrectnessGrader`, `RelevanceGrader`, `HallucinationGrader`) |
| **Single Step** | Did one decision/action/tool-call stand up on its own? | Step-level agent graders (action, tool, memory, plan, reflection, observation) |
| **Trajectory** | Taken as a sequence, is the execution path sound / efficient? | Trajectory graders (`TrajectoryAccuracyGrader`, `TrajectoryComprehensiveGrader`) |

These are **complementary**, not alternatives. A good pipeline includes at least one grader from each granularity.

- Final-Response tells you **whether** the agent succeeded.
- Single-Step tells you **where** it went wrong.
- Trajectory tells you **how efficiently** it got there.

An agent can ace Final-Response while failing Trajectory (correct answer via 47 flailing tool calls) or ace Trajectory while failing Final-Response (elegant execution of the wrong plan). Each hides the other's failure mode.

### Axis 2 — Cognitive module: what part of the agent's cognition?

Inherited from the AgentErrorTaxonomy (ALFWorld/WebShop/GAIA failure analysis). OpenJudge mirrors it in `openjudge/graders/agent/`:

| Module | Subdir | Key graders | What it scores |
|---|---|---|---|
| **Action** | `action/` | `ActionAlignmentGrader`, `ActionLoopDetectionGrader` | Did action match plan? Is agent looping? |
| **Tool** | `tool/` | `ToolSelectionGrader`, `ToolCallAccuracyGrader`, `ToolCallSuccessGrader`, `ToolParameterCheckGrader`, `ToolCallStepSequenceMatchGrader`, `ToolCallPrecisionRecallMatchGrader` | Right tool? Right arguments? Technically succeeded? Matches reference trace? |
| **Memory** | `memory/` | `MemoryAccuracyGrader`, `MemoryDetailPreservationGrader`, `MemoryRetrievalEffectivenessGrader` | Is what the agent remembers true? Details kept? Retrieved when needed? |
| **Plan** | `plan/` | `PlanFeasibilityGrader` | Is the stated plan causally sound and executable? |
| **Reflection** | `reflection/` | `ReflectionAccuracyGrader`, `ReflectionOutcomeUnderstandingGrader`, `ReflectionProgressAwarenessGrader` | Does agent correctly interpret its own progress and outcomes? |
| **Observation** | `observation/` | `ObservationInformationGainGrader` | Gathering novel information, or redundantly re-observing? |
| **Trajectory** | `trajectory/` | `TrajectoryAccuracyGrader`, `TrajectoryComprehensiveGrader` | Holistic sequence-level assessment spanning all modules |

**Key insight on step-level scales:** Single-Step graders split into two camps:

- **Binary {0, 1}** — `ActionAlignmentGrader`, `ToolCallSuccessGrader`, `ToolParameterCheckGrader`, `PlanFeasibilityGrader`, all `Memory*` and `Reflection*` graders. Pass/fail per step. Aggregating binaries across steps gives a *pass rate per cognitive module* — the diagnostic artifact you want.
- **Continuous [0, 1]** — `ActionLoopDetectionGrader` (signature-similarity ratio), `ObservationInformationGainGrader` (novelty ratio). These are code-based (`BaseGrader` subclasses, zero LLM cost) and return fractional values.

Mixing these in one aggregator is usually fine because they share range; mixing with `ToolSelectionGrader` (1-5) or `TrajectoryAccuracyGrader` (1-3) is not.

**Caveat on `ActionLoopDetectionGrader`:** it compares *string signatures* of `tool_calls`. If the agent calls the same tool with slightly different args each step (e.g., different timestamps, different pagination cursors), semantic loops are invisible. Always pair with `ObservationInformationGainGrader` to catch the cases where surface args differ but no new info is acquired.

## Decision matrix — symptom to graders

| If your agent fails because... | Use these graders |
|---|---|
| It gives a factually wrong final answer | `CorrectnessGrader` (final) |
| It never finishes, times out, or loops | `TrajectoryAccuracyGrader` + `ActionLoopDetectionGrader` |
| It picks wrong tools | `ToolSelectionGrader` + `ToolCallAccuracyGrader` |
| It passes wrong arguments to the right tool | `ToolParameterCheckGrader` |
| Tools fail technically (timeouts, errors) | `ToolCallSuccessGrader` |
| It forgets or hallucinates what it observed | `MemoryAccuracyGrader` + `MemoryDetailPreservationGrader` |
| It doesn't use what it already knows | `MemoryRetrievalEffectivenessGrader` |
| Its plan is impossible given state | `PlanFeasibilityGrader` |
| It misjudges what happened | `ReflectionOutcomeUnderstandingGrader` |
| It thinks it's almost done when it isn't | `ReflectionProgressAwarenessGrader` |
| It wastes calls on redundant observations | `ObservationInformationGainGrader` |
| You need one number for end-to-end quality | `TrajectoryComprehensiveGrader` (4 dims, [0, 1]) |
| Compare two agents against a reference tool sequence | `ToolCallStepSequenceMatchGrader` (step-aligned) |

## Data-shape requirements — frequent mistake

Different granularities expect **different fields** in the input dict. Passing the wrong shape is one of the most common errors.

### Final-Response — flat fields

```python
{"query": "...", "response": "...", "reference_response": "..."}
```

### Single-Step — step snapshot (NOT messages-shaped)

```python
# Action grader:
{"plan": "I will open drawer 1", "action": "open drawer 1", "context": "..."}

# Memory graders:
{"observation": "...", "memory": "...", "history": [...]}

# Tool grader:
{"query": "...", "tool_definitions": [...], "tool_calls": [...]}
```

### Trajectory — OpenAI-style messages

```python
{"messages": [
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [...]},
    {"role": "tool",      "tool_call_id": "1", "content": "..."},
    {"role": "assistant", "content": "..."},
]}
```

Two subtleties with trajectory shape:

- `TrajectoryComprehensiveGrader` **automatically strips system prompts** from messages before scoring.
- `TrajectoryAccuracyGrader` does **not** strip — a huge system prompt counts against the efficiency score.

Use a `mapper` (dict or callable) in your `GraderConfig` to produce these shapes from your dataset's native keys. Don't try to unify your dataset to one shape — each grader family wants its own.

## The `AgenticGrader` identity question

Frequent confusion: **`AgenticGrader` is not an agent grader.** It is a grader that uses an agent to evaluate. The target of evaluation can be anything — a chat response, a document, an image.

| Class | What it is | Target of evaluation |
|---|---|---|
| `AgenticGrader` (at `openjudge.graders.agentic_grader`) | A judge that uses its own ReAct loop + tools | Anything |
| `ToolSelectionGrader` (at `openjudge.graders.agent.tool.tool_selection`) | An `LLMGrader` that reads tool definitions and calls | An agent's tool-use |
| `TrajectoryComprehensiveGrader` (at `openjudge.graders.agent.trajectory.*`) | An `LLMGrader` that reads full message trajectory | An agent's execution |

Lookup cue:

- `openjudge.graders.agent.*` → graders **of** agents. Most subclass `LLMGrader`, not `AgenticGrader`. They do not spawn tools of their own.
- `openjudge.graders.agentic_grader.AgenticGrader` → a grader that **acts like** an agent.

### `AgenticGrader` v0.2.0 "unified interface" refactor

**`AgenticGrader` no longer accepts `model=...` / `tools=[...]`** in the constructor. Since v0.2.0, you must build the agent first and pass a pre-built `BaseAgent`:

```python
from openjudge.agentic import ReActAgent
from openjudge.graders.agentic_grader import AgenticGrader

agent = ReActAgent(
    model={"model": "gpt-4o", "api_key": os.environ["OPENAI_API_KEY"]},
    tools=[WebSearchTool()],     # your BaseTool subclasses
    max_iterations=6,
)
grader = AgenticGrader(agent=agent, template="Verify: {response}")
```

Older tutorials show `AgenticGrader(model=..., tools=[...])`. That path is dead — the refactor exists specifically so you can swap in a LangChain or AgentScope agent via an adapter.

**Cost warning:** each `aevaluate` spawns up to `max_iterations` LLM calls + tool calls. 100 samples × 6 iterations × ~2 LLM calls ≈ 1200+ LLM calls. Low `max_concurrency` (2-4). Log spend.

## External-framework adapters — where they live

OpenJudge ships *references* to `AgentScopeAgentAdapter` and `LangChainAgentAdapter`, but the **implementations are not in the `openjudge` package**. They live in `cookbooks/agentic_grader/adapters/`.

```python
# This imports nothing usable:
from openjudge.agentic.adapters import LangChainAgentAdapter   # wrong

# This is correct — requires the repo to be checked out (or copy the adapter):
from cookbooks.agentic_grader.adapters.langchain import LangChainAgentAdapter
from cookbooks.agentic_grader.adapters.agentscope import AgentScopeAgentAdapter
```

The `openjudge/agentic/__init__.py` docstring even says so — the separation exists to avoid circular dependencies. Don't trust model-prior autocomplete here.

### Three adapter traps

**1. `AgentScopeToolAdapter` rejects `Toolkit` instances.** It only wraps individual registered tool functions. Extract first:

```python
# Wrong: AgentScopeToolAdapter(toolkit)  → ValueError
# Right:
my_tool = toolkit.tools['my_tool']
adapted = AgentScopeToolAdapter(my_tool)
```

**2. `AgenticGrader.from_config` hardcodes `ReActAgent`.** It cannot reconstruct a LangChain- or AgentScope-wrapped agent from YAML. Use the constructor directly, and persist the construction code out-of-band alongside any config file.

**3. Adapters may drift with upstream versions.** Because they live in cookbooks (not the versioned package), a new LangChain/AgentScope release can break them without a package bump. Pin adapter-dependent workflows to a known-good cookbook commit.

## Agent-grader scales — the aggregation minefield

Agent pipelines routinely mix all four scales in one aggregator — severer than general grader use. Full scale table and normalization formulas live in `openjudge-grader-selection`. Short version: binary/continuous in [0,1], `ToolSelectionGrader`/`ToolCallAccuracyGrader` in 1-5, `TrajectoryAccuracyGrader` in **1-3**, `TrajectoryComprehensiveGrader` in [0,1] (its prompt asks for 1-5, the callback normalizes — `GraderScore.score` is the post-normalization value).

The two trajectory siblings have **different external scales**; they look like siblings and aren't interchangeable.

### Why naive aggregation fails for agents

`WeightedSumAggregator` multiplies raw `score` by weight and sums (`openjudge/runner/aggregator/weighted_sum_aggregator.py:62-82`). No scale check. Consider a realistic agent pipeline:

```
correctness (1-5)   × 0.4   → up to 2.0
tool_sel (1-5)      × 0.2   → up to 1.0
trajectory (0-1)    × 0.3   → up to 0.3
plan (binary)       × 0.1   → up to 0.1
```

Correctness dominates by construction; adjusting the binary/continuous weights barely moves the result. Users attribute this to "grader noise".

**Trajectory-sibling worked trap.** `TrajectoryAccuracyGrader` (1-3) + `TrajectoryComprehensiveGrader` ([0,1]) at equal weight 0.5, healthy pipeline means Accuracy=2.4, Comprehensive=0.65:

```
composite:  0.5 * 2.4 + 0.5 * 0.65 = 1.525
Accuracy contributes:      1.2    (~79%)
Comprehensive contributes: 0.325  (~21%)
```

The 1-3 sibling is **3.7x louder** than its 0-1 sibling — "50-50" becomes ~4-to-1 by scale alone. Preference order for fixing: **drop > normalize > always-smoke-test**. Dropping one trajectory grader is usually correct because they measure overlapping signals; if you normalize, use `(x-1)/2` (denominator 2 for 1-3, not 4).

### The only safe rule — normalize before aggregating

Wrap raw graders in `FunctionGrader` normalizers. Formulas:

```python
# 1-5  → [0, 1]:  (x - 1) / 4
# 1-3  → [0, 1]:  (x - 1) / 2
# binary / [0, 1]:  identity
# min-max int:  (x - min) / (max - min)
# ALWAYS clamp first: score = max(lo, min(hi, float(x)))
```

See `openjudge-rubric-workflow` for the full aggregation pattern and the `GraderError` exclusion pitfall (errored graders leave the denominator, shifting composites per-sample).

### Second landmine: `WeightedSumAggregator` drops `GraderError`

A sample where `tool_selection` errored gets its composite computed over only
the survivors. Different samples → different surviving sets → cross-sample
composites non-comparable. The aggregator does not record which graders were
skipped per sample. Fix: custom aggregator that returns `GraderError` if any
input errored, or substitutes a worst-case value.

## Reference template — complete agent pipeline

```python
from openjudge.runner import GradingRunner
from openjudge.runner.aggregator import WeightedSumAggregator
from openjudge.graders.common import CorrectnessGrader
from openjudge.graders.agent.tool.tool_selection import ToolSelectionGrader
from openjudge.graders.agent.trajectory.trajectory_comprehensive import TrajectoryComprehensiveGrader

model = OpenAIChatModel(model="qwen3-max")

grader_configs = {
    # Final-Response axis
    "correctness":    {"grader": CorrectnessGrader(model=model),
                       "mapper": {"query": "q", "response": "a", "reference_response": "ref"}},
    # Single-Step axis (tool module)
    "tool_selection": {"grader": ToolSelectionGrader(model=model),
                       "mapper": {"query": "q", "tool_definitions": "td", "tool_calls": "tc"}},
    # Trajectory axis
    "trajectory":     {"grader": TrajectoryComprehensiveGrader(model=model),
                       "mapper": {"messages": "msgs"}},
}

# CAUTION: correctness is 1-5, tool_selection is 1-5, trajectory is [0, 1].
# A naive WeightedSumAggregator here produces a meaningless composite.
# Normalize first (see openjudge-grader-selection), or report the three
# dimensions SEPARATELY rather than as a single number.
runner = GradingRunner(grader_configs=grader_configs, max_concurrency=8)
results = await runner.arun(dataset)
```

The caution is not rhetorical. It is the single most common mistake when combining agent graders.

## Agent-specific anti-patterns

1. **Only Final-Response graders.** Hides whether the agent was lucky or competent.
2. **Only per-step graders.** Aggregating step accuracy doesn't tell you if the final answer was right.
3. **Mixing `TrajectoryAccuracyGrader` (1-3) + `TrajectoryComprehensiveGrader` ([0,1])** in one aggregator without normalization — same name space, different scales, 1-3 sibling dominates ~3.7x.
4. **Flat-dumping messages to `ToolSelectionGrader`** — it wants `query` + `tool_definitions` + `tool_calls` separately. Use a callable mapper.
5. **`TrajectoryComprehensiveGrader` on 2-step trajectories** — averages across steps; small N → high variance. Prefer `TrajectoryAccuracyGrader` for short trajectories.
6. **Expecting `ActionLoopDetectionGrader` to catch semantic loops** — it compares string signatures. Combine with `ObservationInformationGainGrader`.
7. **Confusing `AgenticGrader` with agent-of-agents.** `AgenticGrader` spawns tools for **the judge**. Scoring an agent that already has tools → `TrajectoryComprehensiveGrader` + tool-module graders.
8. **`from openjudge.agentic.adapters import ...`** — imports nothing usable. Adapters are at `cookbooks/agentic_grader/adapters/{langchain,agentscope}.py`.
9. **`AgenticGrader.from_config` with a LangChain/AgentScope agent** — hardcodes `ReActAgent`; silently returns a different grader.

## Cross-skill pointers

- `openjudge-grader-selection` — base-class decision tree, full scale map, `LLMGrader` internal traps (structured_model downgrade, fallback chain, callback, bilingual templates).
- `openjudge-rubric-workflow` — `GradingRunner` lifecycle, aggregator traps, rubric generation for custom agent-task graders.
- `openjudge-index` — rename/refactor debiasing and cross-skill routing.
