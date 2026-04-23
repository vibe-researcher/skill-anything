# skill-anything

**[中文版](README_zh.md) | English**

> An **Agent-Skill Compiler**: point it at a GitHub repo, get back a set of Skills your agent can load like it's already an expert.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Claude%20Code%20%7C%20Cursor%20%7C%20any%20agent-lightgrey)]()
[![Status: beta](https://img.shields.io/badge/status-beta-orange)]()

---

## What this gives you

- **Skills, not wrappers.** The output is [Anthropic-standard `SKILL.md`](https://docs.anthropic.com/en/docs/claude-code/skills) bundles — usable on Claude Code, Cursor, and 27+ other agent platforms. No runtime, no SDK.
- **Graded with receipts.** Every published Skill ships with a blind A/B eval manifest: grader scores, blind mapping, and honest caveats. Third parties can re-run the evaluation.
- **Honest about failure.** Our v1 loop cheated its own eval. We shipped the [postmortem](docs/why-v2.md) and the [original 16-problem critique](critique-report.json) alongside the code.

This is the part of the agent stack between "prompt template" and "full framework": **compiled domain knowledge that the agent loads on demand.**

---

## Try this now

A distilled Skill for [OpenJudge](https://github.com/agentscope-ai/OpenJudge) (LLM-as-judge evaluation framework) is already published in this repo. One command to install it into your Claude Code:

```bash
git clone https://github.com/vibe-researcher/skill-anything
cd skill-anything
python3 scripts/register_skill.py \
  published/openjudge-{index,grader-selection,rubric-workflow,agent-eval} \
  --to user
```

Your agent now has four new skills for picking OpenJudge graders, normalizing score scales, running rubric workflows, and evaluating agent trajectories. Ask it *"which OpenJudge grader for factual-QA?"* and watch the answer improve.

Non-bash shell? `cp -r published/openjudge-{index,grader-selection,rubric-workflow,agent-eval} ~/.claude/skills/` also works.

---

## Receipts (what "graded" actually means)

Most projects in this space claim quality; we'd rather show the audit trail.

**OpenJudge distillation, iter-2 (kept), 2026-04-23:**

| Metric | Value | Notes |
|---|---|---|
| Composite score | **0.9167** | quality 0.6 + trajectory 0.4, but trajectory was unavailable this run — effectively quality-only |
| Skill-won rate | 12/12 tasks | vs no-Skill baseline |
| Convergence | ✅ | Δ < 0.03 between iter-1 and iter-2 |
| iter-3 | 0.7792 (discarded) | regression — published as `status: beta` |
| Grader ensemble | K=1 | cost trade-off; K=3 is the recommended default |
| Pending surprises | 8 unresolved | see [lessons-learned](docs/lessons-learned-openjudge-2026-04-23.md) |

Raw artifacts: [`published/openjudge-receipts/`](published/openjudge-receipts/) — iteration summaries, grader scores, blind mapping, OSR digests, SHA-256 file hashes. Full [`eval-manifest.json`](published/openjudge-receipts/eval-manifest.json) documents how to reproduce the run.

**Why we think this matters.** Agent-graded-by-agent is the default in our field, and silent failure looks exactly like success. The only honest response is to publish the receipts — see [`docs/why-v2.md`](docs/why-v2.md) for the full story of how our v1 loop cheated its own eval.

---

## Gallery

One skill bundle published so far. More coming — [request one](.github/ISSUE_TEMPLATE/distillation-request.yml) or contribute your own run.

| Bundle | Target | Sub-skills | Best score | Status |
|---|---|---|---|---|
| [`openjudge`](published/openjudge-index/) | [agentscope-ai/OpenJudge](https://github.com/agentscope-ai/OpenJudge) | 4 | 0.92 | `beta` |

Sub-skills under `openjudge`:

- [`openjudge-index`](published/openjudge-index/SKILL.md) — routing + debiasing (`rm-gallery` → `py-openjudge`, `Reward` → `Grader`). Read first.
- [`openjudge-grader-selection`](published/openjudge-grader-selection/SKILL.md) — LLMGrader vs FunctionGrader vs AgenticGrader decision tree; score-scale map (1-3 vs 1-5 vs binary); `structured_model` traps.
- [`openjudge-rubric-workflow`](published/openjudge-rubric-workflow/SKILL.md) — three-path rubric generation; `GradingRunner` batch eval; `IterativeRubricsGenerator` silent-empty-rubric gotcha.
- [`openjudge-agent-eval`](published/openjudge-agent-eval/SKILL.md) — TrajectoryGrader (1-3) vs ToolCallAccuracyGrader (1-5) with explicit normalization formulas; AgenticGrader v0.2.0 pre-built agent requirement.

---

## Distill a new repo

Open this repo in Claude Code and say:

```
Distill the domain knowledge of <repo-url> into Agent Skills
```

Claude reads [`SKILL.md`](SKILL.md), creates a `workspace/`, and drives the full autonomous loop: research → generate → blind eval → score → iterate → converge. You can interrupt at any time; state is durable in `workspace/orchestrator-state.json`.

Published output goes to `workspace/skills/`. To promote to the repo's `published/` directory, `cp -r` it and add an entry to `registry.json` — see the OpenJudge entry as a template.

**Expect a single run to consume several million tokens** across Researcher, Skill Writer, Eval Designer, Runners, and Graders. Cost telemetry is on the [roadmap](#roadmap) but not instrumented yet.

---

## How it works (30-second version)

```
Input:  GitHub repo URL
Output: 1-5 SKILL.md files  ←  your agent loads these, not the repo

Loop:
  Researcher × N  →  knowledge/*.md
  Skill Writer    →  skills/*/SKILL.md
  Eval Designer   →  evals/eval-tasks.json   (isolated — never sees the Skills)
  Runner × 2/task →  with-Skill vs baseline outputs
  Blind mapping   →  randomized A/B labels
  Grader × K      →  blind scores + rationale (ensemble, majority vote)
  Score           →  composite = 0.6·quality + 0.4·trajectory
  Improve or stop →  Δ < 0.03 for 2 rounds = converged
```

For the full design — physical worktree isolation, OSR structured returns, the nine guardrails, Markov-style resumability — see [`docs/concepts.md`](docs/concepts.md).

---

## Design principles

**Claude as Orchestrator, not pipeline.** Complexity lives in natural-language instructions, not in a build chain. Zero Python dependencies in helper scripts. Identical behavior across every Claude-compatible platform.

**Physical isolation, not protocol isolation.** Runners, Graders, and Eval Designers each run in isolated git worktrees — the Orchestrator *cannot* play their roles even if the prompt tries to let it. This constraint exists because [v1 proved protocol isolation isn't](docs/why-v2.md).

**Simplicity bias.** A Skill that produces equivalent results in fewer words is strictly better. Iterations may discard content if scores don't improve.

---

## Roadmap

- **Reproducible Blind-Eval Protocol** — formalize the manifest + re-run CLI into a citable standard. Currently the `eval-manifest.json` format is a draft; we want a schema and a `reproduce.py` that re-grades a published run against its frozen grader prompts.
- **Cost telemetry** — emit real token/dollar counts per iteration into `cost-report.json`, feed into the README gallery.
- **`skill_diff`** — compare Skills distilled from two versions of the same repo. Surface the upgrade-gotchas that upstream changelogs forget.
- **`skill_compose`** — combine multiple Skills into a composite workflow Skill (analogous to [cli-anything](https://github.com/vibe-researcher/cli-anything)'s `cli-hub install`).
- **Automated regeneration on upstream release** — GitHub Action that re-distills when the target repo tags a new version and opens a PR to `registry.json`.
- **Multi-grader re-run of OpenJudge** — the shipped run used K=1 for cost. Re-run at K=3 to move from `beta` to `stable`.

---

## Project layout

```
skill-anything/
├── SKILL.md                        # Orchestrator workflow (Claude reads this)
├── agents/                         # Role guides: researcher, skill-writer, grader, ...
├── scripts/                        # Pure stdlib Python helpers (27 scripts)
│   ├── register_skill.py           # Install a Skill bundle to ~/.claude/skills/
│   ├── validate_skill.py           # Format check
│   ├── blind_eval.py               # Randomize A/B labels for grading
│   ├── deblind_and_score.py        # Unblind + composite score + convergence
│   └── ...
├── schemas/                        # OSR return schemas + state schema
├── references/eval-loop.md         # Full iteration protocol
├── docs/
│   ├── concepts.md                 # v2 harness deep-dive (OSR, Markov, guardrails)
│   ├── why-v2.md                   # Postmortem: how our v1 agent cheated its own eval
│   ├── lessons-learned-openjudge-2026-04-23.md
│   └── harness-design.md
├── published/                      # Distilled Skills ready to install
│   ├── openjudge-{index,grader-selection,rubric-workflow,agent-eval}/
│   └── openjudge-receipts/         # eval manifest + per-iter summaries + grader scores
├── registry.json                   # Index of published Skills
├── catalog-skill/                  # Agent-discoverable pointer to the catalog
├── critique-report.json            # v1 self-assessment (16 problems, public)
└── workspace/                      # Created per distillation run (gitignored)
```

---

## Contributing

- **Request a distillation target** — [open an issue](.github/ISSUE_TEMPLATE/distillation-request.yml) with the repo URL and what tacit knowledge it has.
- **Run a distillation yourself** — the loop is autonomous; if the output looks good, PR it into `published/` with an entry in `registry.json`.
- **Improve eval tasks** — sharper eval tasks have higher discriminative power. If you see a weak or redundant task, flag it.
- **Script improvements** — keep them stdlib-only. No Node.js, no heavy dependencies.

Large changes: please open an issue first.

---

## Citation

```bibtex
@software{skill-anything,
  author  = {vibe-researcher},
  title   = {skill-anything: Agent-Skill Compiler with Reproducible Blind Evaluation},
  year    = {2026},
  url     = {https://github.com/vibe-researcher/skill-anything},
  license = {Apache-2.0}
}
```

---

## License

Copyright 2026 vibe-researcher — [Apache License 2.0](LICENSE).

## Acknowledgments

- [Anthropic](https://anthropic.com) — for the SKILL.md standard and Claude Code platform.
- [OpenJudge (agentscope-ai)](https://github.com/agentscope-ai/OpenJudge) — the distillation target used throughout this README's examples and receipts.
- The broader agent-evaluation community whose prior work on blind evaluation and trajectory scoring informed this design.
