# skill-anything

**[中文版](README_zh.md) | English**

> Distill any GitHub repository into high-quality Agent Skills through autonomous research, generation, and blind evaluation.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Claude%20Code%20%7C%20Cursor%20%7C%20any%20agent-lightgrey)]()

---

## What Is This?

`skill-anything` is an **autonomous knowledge distillation system**. Give it a GitHub repo URL; it produces a set of [Agent Skill files](https://docs.anthropic.com/en/docs/claude-code/skills) that let any Claude-based agent instantly reason about that repository's domain — without reading the source code.

**Not a documentation extractor.** The goal is *knowledge distillation*: turning scattered, human-oriented content into a form an agent can directly use as internalized capability. An agent that reads a Skill should "understand the domain" — making correct decisions from first principles, not just knowing how to call an API.

```
Input:  GitHub repo URL
Output: SKILL.md files  ←  agent loads these, not the repo
```

### Why It Matters

An agent's capability ceiling = model intelligence × available context.

High-value repos are often the hardest to reason about:
- Training data is outdated (Tailwind CSS v4, Next.js App Router)
- Tacit knowledge was never documented (MCP SDK, Pydantic v2)
- Version-specific gotchas dominate support questions (OpenJudge grader scaling)

Skills solve all three — and stay current because you regenerate them.

---

## Features

- **Fully autonomous loop** — Research → Generate → Blind Eval → Score → Improve → Convergence, all without human intervention
- **Claude as Orchestrator** — no SDK build chain, no Node.js, just `SKILL.md` instructions that work on any agent platform
- **Blind evaluation** — Grader sees randomized A/B outputs; never knows which used the Skill
- **Dual-signal scoring** — quality (output depth & correctness) + trajectory (tool call efficiency)
- **Convergence detection** — automatically stops when improvement plateaus (Δs < 0.03 for 2 rounds)
- **Zero Python dependencies** — all helper scripts use stdlib only
- **Anthropic SKILL.md compatible** — output works on Claude Code, Cursor, and 27+ agent platforms

---

## How It Works

```
┌──────────────────────────────────────────────────┐
│  Claude (Orchestrator)                           │
│  Reads SKILL.md → plans research directions      │
│  Spawns sub-agents → reviews outputs → decides   │
└─────────────────────┬────────────────────────────┘
                      │
       ┌──────────────┼────────────────┐
       ▼              ▼                ▼
  Researcher×N   Skill Writer    Eval Designer
  (parallel)     (in: reports    (isolated: never
  knowledge/     + feedback)      sees Skills)
                 out: skills/
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
     Runner+Skill            Runner (baseline)
     (repo + Skill)          (repo only)
          │                       │
          └──────────┬────────────┘
                     ▼
                  Grader
               (blind A/B)
                     │
                     ▼
            composite score
          → next iteration or
              converge
```

**Composite score formula:**

```
composite = (quality/5) × 0.6  +  trajectory × 0.4
trajectory = max(0, 1 − toolCalls_with / toolCalls_without)
```

Both runners have repo access. Trajectory difference directly measures distillation efficiency.

---

## Quick Start

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (or any Claude-based agent)
- Python 3.10+ (for helper scripts)
- Git

### Setup

```bash
git clone https://github.com/vibe-researcher/skill-anything
cd skill-anything
mkdir workspace
```

### Run a Distillation

Open `skill-anything` in Claude Code and run:

```
Distill the domain knowledge of <repo-url> into Agent Skills
```

Claude reads `SKILL.md`, creates a `workspace/`, and drives the full loop autonomously. You can interrupt and resume at any time — state is saved in `workspace/orchestrator-state.json`.

### Helper Scripts

All scripts are pure stdlib Python:

| Script | Purpose | Usage |
|--------|---------|-------|
| `blind_eval.py` | Randomize A/B labels for blind grading | `python scripts/blind_eval.py <workspace> <iter>` |
| `deblind_and_score.py` | Unblind + compute composite score + check convergence | `python scripts/deblind_and_score.py <workspace> <iter>` |
| `validate_skill.py` | Validate Skill directory format | `python scripts/validate_skill.py <skill-dir>` |
| `gen_viewer.py` | Generate HTML eval viewer | `python scripts/gen_viewer.py <workspace> [iter]` |
| `register_skill.py` | Publish Skill to registry | `python scripts/register_skill.py <workspace> <repo-url>` |

---

## Example: Distilling OpenJudge

This case study shows `skill-anything` distilling [OpenJudge](https://github.com/agentscope-ai/OpenJudge) — an LLM evaluation framework — across 5 iterations.

### Setup

OpenJudge is a strong distillation target: it has implicit scaling rules (TrajectoryGrader uses a 1–3 scale, ToolCallAccuracyGrader uses 1–5), silent failure modes (wrong field name → all rubrics discarded silently), and non-obvious composition patterns. Exactly the kind of tacit knowledge that hurts agents in practice.

### Iteration History

| Iter | Composite | Δ | Key improvement |
|------|-----------|---|----------------|
| 1 | 0.5563 | — | Initial generation |
| 2 | 0.6022 | +0.046 | Score scale normalization docs |
| 3 | 0.7109 | +0.109 | GraderError silent distortion, AgenticGrader tool format fix |
| 4 | 0.8382 | +0.127 | Agent grader dimensional normalization formulas |
| **5** | **0.8622** | **+0.024** | **Converged** — FunctionGrader wrapping, two-stage threshold table, `structured_model` config |

**Convergence:** `score_converged` after iter-5 (Δ = 0.024 < 0.03). All 12 tasks `skillWon`, zero regressions.

### What the Skill Captures

The distillation produced three Skill files:

**`openjudge-grader-selection`** — grader decision tree, score scale table, `structured_model` Pydantic config, GraderError rate monitoring pattern

**`openjudge-rubric-workflow`** — three-path rubric generation, GradingRunner batch evaluation, two-stage strategy with task-type → filter-rate table, FunctionGrader normalization wrapper

**`openjudge-agent-eval`** — TrajectoryGrader (1–3 scale) vs ToolCallAccuracyGrader (1–5 scale) with explicit normalization:
```python
trajectory_norm = (trajectory_result.score - 1) / 2    # 1-3 → 0-1
tool_call_norm  = (tool_call_result.score - 1) / 4      # 1-5 → 0-1
composite = 0.6 * trajectory_norm + 0.4 * tool_call_norm
```

### Before / After

**Task:** "Evaluate an agent trajectory. Available graders: ToolCallAccuracyGrader, TrajectoryComprehensiveGrader, OutcomeGrader. Choose and compose."

Without Skill (4 tool calls):
> "ToolCallAccuracyGrader for single-step accuracy, TrajectoryAccuracyGrader for full path. Combine them."

With Skill (1 tool call):
> "TrajectoryComprehensiveGrader (scale **1–3**, not 1–5) as primary (weight 0.6) + ToolCallAccuracyGrader (scale **1–5**) secondary (weight 0.4). Normalize before aggregating: `trajectory_norm=(score-1)/2`, `tool_call_norm=(score-1)/4`. Composite = `0.6×trajectory_norm + 0.4×tool_call_norm`. If choosing one: TrajectoryComprehensiveGrader covers full-path quality."

The skill-less output is plausible but missing the scale mismatch — a silent bug in production.

---

## Project Structure

```
skill-anything/
├── SKILL.md                    # Orchestrator workflow (Claude reads this)
├── agents/
│   ├── researcher.md           # Researcher role guide
│   ├── skill-writer.md         # Skill Writer role guide
│   ├── eval-designer.md        # Eval Designer role guide
│   └── grader.md               # Grader role guide
├── scripts/                    # Pure stdlib Python helpers
│   ├── blind_eval.py
│   ├── deblind_and_score.py
│   ├── validate_skill.py
│   ├── gen_viewer.py
│   ├── register_skill.py
│   ├── generate_catalog.py
│   └── ...
├── references/
│   └── eval-loop.md            # Detailed iteration loop spec
├── catalog-skill/
│   └── SKILL.md                # Points agents to the published catalog
├── registry.json               # Published Skills index
├── published/                  # Published Skill files
└── workspace/                  # Created per distillation run
    ├── orchestrator-state.json
    ├── knowledge/              # Researcher outputs
    ├── skills/                 # Generated Skills
    └── evals/                  # Eval tasks & results
```

---

## Skill Output Format

Skills follow the [Anthropic SKILL.md standard](https://docs.anthropic.com/en/docs/claude-code/skills), compatible with 27+ agent platforms:

```yaml
---
name: my-skill
description: >-
  What this skill does and when to use it. (≤1024 chars)
metadata:
  author: skill-anything
  version: "1.0"
  sa-source-repo: "https://github.com/org/repo"
  sa-generated-at: "2026-04-14T00:00:00Z"
  sa-eval-score: "0.86"
---
# Skill content (< 500 lines)
...
```

Custom metadata uses `sa-` prefix (non-invasive, standard-compatible).

---

## Publishing Skills

```bash
# Register a distilled Skill to the catalog
python scripts/register_skill.py workspace/ https://github.com/org/repo

# Regenerate catalog
python scripts/generate_catalog.py

# Publish
git add published/ registry.json catalog-skill/
git commit -m "publish: repo-name skills"
git push
```

Agents discover Skills through a three-level hierarchy: pointer (40 lines) → catalog index (100 lines) → full Skill (on demand).

---

## Design Principles

**Claude as Orchestrator, not pipeline.** Complexity lives in natural-language instructions, not code. This means zero build-chain dependencies and identical behavior across all Claude-compatible platforms.

**Evaluation isolation.** Eval Designer works in a physically isolated directory — it cannot see the Skills it will be testing. Grader receives randomized A/B labels. Both guarantees are architectural, not just procedural.

**Simplicity bias.** A Skill that produces equivalent results in fewer words is strictly better. Every iteration can discard content if it doesn't improve scores.

---

## Contributing

Contributions are welcome. Areas where help is most valuable:

- **New target repos** — open an issue with the repo URL and why it's a good distillation target
- **Eval task quality** — better eval tasks that have higher discriminative power
- **Script improvements** — the helper scripts are intentionally minimal; PRs that keep them stdlib-only are preferred
- **Platform testing** — verifying Skill compatibility with non-Claude-Code agents

Please open an issue before large changes. Keep PRs focused.

---

## Citation

If you use `skill-anything` in research or build on this work, please cite:

```bibtex
@software{skill-anything,
  author  = {vibe-researcher},
  title   = {skill-anything: Autonomous Knowledge Distillation from GitHub Repos to Agent Skills},
  year    = {2026},
  url     = {https://github.com/vibe-researcher/skill-anything},
  license = {Apache-2.0}
}
```

---

## License

Copyright 2026 vibe-researcher

Licensed under the [Apache License, Version 2.0](LICENSE). You may not use this project except in compliance with the License.

---

## Acknowledgments

- [Anthropic](https://anthropic.com) for the SKILL.md standard and Claude Code platform
- [OpenJudge](https://github.com/agentscope-ai/OpenJudge) for serving as the primary case-study repo in this README
- The broader agent evaluation community whose work on blind evaluation and trajectory scoring informed this design
