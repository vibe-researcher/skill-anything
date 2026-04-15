---
name: index
description: >-
  OpenJudge 知识蒸馏 Skill 索引。包含三个 Skill，覆盖 grader 选型、rubric 生成与批量评估工作流、
  Agent 评估与外部集成。初次使用时从此索引导航到对应 Skill。
metadata:
  author: skill-anything
  version: "1.0"
  sa-source-repo: "https://github.com/agentscope-ai/OpenJudge"
  sa-generated-at: "2026-04-14T00:00:00Z"
---

# OpenJudge Skill 索引

## Skill 列表

| Skill | 核心问题 | 依赖 |
|-------|---------|------|
| [openjudge-grader-selection](../openjudge-grader-selection/SKILL.md) | 选哪种 grader？score 怎么合并？ | 无 |
| [openjudge-rubric-workflow](../openjudge-rubric-workflow/SKILL.md) | 如何生成 rubric？如何批量评估多维度？ | grader-selection |
| [openjudge-agent-eval](../openjudge-agent-eval/SKILL.md) | 如何评估 Agent 轨迹？如何接入 LangSmith/Langfuse/VERL？ | grader-selection, rubric-workflow |

## 导航指引

**第一次用 OpenJudge**：先读 `openjudge-grader-selection`，建立 grader 类型体系认知。

**需要为新任务建立评估标准**：读 `openjudge-rubric-workflow`（三路径决策 + 工作流）。

**评估 Agent 的工具调用 / 轨迹质量**：读 `openjudge-agent-eval`。

**接入 LangSmith / Langfuse / VERL**：读 `openjudge-agent-eval` 的外部集成部分。
