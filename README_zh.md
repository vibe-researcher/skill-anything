# skill-anything

**中文版 | [English](README.md)**

> 通过自主研究、生成与盲法评估，将任意 GitHub 仓库蒸馏为高质量 Agent Skill。

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Claude%20Code%20%7C%20Cursor%20%7C%20任意%20Agent-lightgrey)]()

---

## 这是什么？

`skill-anything` 是一个**自主知识蒸馏系统**。输入一个 GitHub 仓库 URL，输出一套 [Agent Skill 文件](https://docs.anthropic.com/en/docs/claude-code/skills)——让任何基于 Claude 的 Agent 无需阅读源码，就能对该仓库的领域知识进行准确推理。

**不是文档提取器。** 目标是*知识蒸馏*：将分散的、面向人类的内容，转化为 Agent 可直接用作内化能力的形式。读完 Skill 的 Agent 应该"理解了这个领域"——基于第一原理做出正确决策，而不只是知道怎么调 API。

```
输入：GitHub 仓库 URL
输出：SKILL.md 文件  ←  Agent 加载这些，而不是仓库本身
```

### 为什么重要

Agent 的能力上限 = 模型智能 × 可用上下文。

高价值的仓库往往是最难推理的：
- 训练数据已过时（Tailwind CSS v4、Next.js App Router）
- 隐性知识从未被文档化（MCP SDK、Pydantic v2）
- 特定版本的陷阱主导了支持问题（OpenJudge grader 量程）

Skill 解决了上面所有三点——而且因为你可以重新生成，它始终保持最新。

---

## 核心特性

- **全自主循环** — 研究 → 生成 → 盲法评估 → 评分 → 改进 → 收敛，全程无需人工干预
- **Claude 即编排者** — 无 SDK 构建链，无 Node.js，只需 `SKILL.md` 指令，适用于任意 Agent 平台
- **盲法评判** — Grader 看到的是随机化的 A/B 输出，永远不知道哪个使用了 Skill
- **双信号评分** — 质量（输出深度与正确性）+ 轨迹（工具调用效率）
- **收敛检测** — 改善停滞时自动停止（Δs < 0.03 连续 2 轮）
- **零 Python 依赖** — 所有辅助脚本仅使用标准库
- **兼容 Anthropic SKILL.md 标准** — 输出可在 Claude Code、Cursor 等 27+ Agent 平台上使用

---

## 工作原理

```
┌──────────────────────────────────────────────────┐
│  Claude（编排者 Orchestrator）                    │
│  读取 SKILL.md → 规划研究方向                     │
│  Spawn 子代理 → 审视产出 → 全局决策               │
└─────────────────────┬────────────────────────────┘
                      │
       ┌──────────────┼────────────────┐
       ▼              ▼                ▼
   研究员×N        技能作者          评估设计师
  （并行）        （输入：报告       （隔离：物理上
  knowledge/      + 反馈）           看不到 Skills）
                  输出：skills/
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
    Runner+Skill            Runner（基线）
    （仓库 + Skill）         （仅仓库）
          │                       │
          └──────────┬────────────┘
                     ▼
                  Grader
               （盲法 A/B）
                     │
                     ▼
             综合评分 composite
           → 下一轮迭代 或 收敛
```

**综合评分公式：**

```
composite = (quality/5) × 0.6  +  trajectory × 0.4
trajectory = max(0, 1 − toolCalls_with / toolCalls_without)
```

两个 Runner 都有仓库访问权，轨迹差异直接衡量蒸馏效率。

---

## 快速开始

### 前置条件

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（或任意基于 Claude 的 Agent）
- Python 3.10+（用于辅助脚本）
- Git

### 安装

```bash
git clone https://github.com/vibe-researcher/skill-anything
cd skill-anything
mkdir workspace
```

### 运行蒸馏

在 Claude Code 中打开 `skill-anything` 项目，执行：

```
将 <repo-url> 的领域知识蒸馏为 Agent Skill
```

Claude 读取 `SKILL.md`，创建 `workspace/`，并自主驱动完整循环。随时可以中断并恢复——状态保存在 `workspace/orchestrator-state.json`。

### 辅助脚本

所有脚本均为纯标准库 Python：

| 脚本 | 用途 | 调用方式 |
|------|------|---------|
| `blind_eval.py` | 随机化 A/B 标签用于盲法评判 | `python scripts/blind_eval.py <workspace> <iter>` |
| `deblind_and_score.py` | 反盲化 + 计算综合评分 + 收敛检查 | `python scripts/deblind_and_score.py <workspace> <iter>` |
| `validate_skill.py` | 验证 Skill 目录格式合规性 | `python scripts/validate_skill.py <skill-dir>` |
| `gen_viewer.py` | 生成 HTML 评估可视化页面 | `python scripts/gen_viewer.py <workspace> [iter]` |
| `register_skill.py` | 将 Skill 发布到 registry | `python scripts/register_skill.py <workspace> <repo-url>` |

---

## 案例：蒸馏 OpenJudge

本案例展示了 `skill-anything` 对 [OpenJudge](https://github.com/agentscope-ai/OpenJudge)（一个 LLM 评估框架）进行 5 轮迭代蒸馏的完整过程。

### 为什么选 OpenJudge

OpenJudge 是极佳的蒸馏目标：它存在隐式量程规则（TrajectoryGrader 使用 1–3 量程，ToolCallAccuracyGrader 使用 1–5 量程）、静默失败模式（字段名错误 → 所有 rubric 被静默丢弃）以及非直觉的组合模式——这些正是实际生产中会坑到 Agent 的隐性知识。

### 迭代历程

| 轮次 | 综合评分 | 变化 | 关键改进 |
|------|---------|------|---------|
| 1 | 0.5563 | — | 初始生成 |
| 2 | 0.6022 | +0.046 | 评分量程归一化文档 |
| 3 | 0.7109 | +0.109 | GraderError 静默失真、AgenticGrader 工具格式修复 |
| 4 | 0.8382 | +0.127 | Agent grader 量纲归一化公式 |
| **5** | **0.8622** | **+0.024** | **收敛** — FunctionGrader 包装归一化、二阶段阈值表、`structured_model` 配置 |

**收敛状态：** iter-5 后 `score_converged`（Δ = 0.024 < 0.03）。全部 12 个任务 `skillWon`，零回归。

### Skill 蒸馏出了什么

本次蒸馏产出三个 Skill 文件：

**`openjudge-grader-selection`** — grader 选型决策树、评分量程速查表、`structured_model` Pydantic 配置、GraderError 率监控模式

**`openjudge-rubric-workflow`** — 三路径 rubric 生成策略、GradingRunner 批量评估工作流、二阶段评估策略（含任务类型→过滤率对应表）、FunctionGrader 归一化包装

**`openjudge-agent-eval`** — TrajectoryGrader（1–3 量程）vs ToolCallAccuracyGrader（1–5 量程），含显式归一化公式：
```python
trajectory_norm = (trajectory_result.score - 1) / 2    # 1-3 → 0-1
tool_call_norm  = (tool_call_result.score - 1) / 4      # 1-5 → 0-1
composite = 0.6 * trajectory_norm + 0.4 * tool_call_norm
```

### 前后对比

**任务：**「评估一个 Agent 轨迹，可选 grader 有：ToolCallAccuracyGrader、TrajectoryComprehensiveGrader、OutcomeGrader。请选择并组合。」

**无 Skill 时**（4 次工具调用）：
> "用 ToolCallAccuracyGrader 评估单步准确性，用 TrajectoryAccuracyGrader 评估完整路径，组合使用即可。"

**有 Skill 时**（1 次工具调用）：
> "TrajectoryComprehensiveGrader（量程 **1–3**，注意不是 1–5）为主（权重 0.6）+ ToolCallAccuracyGrader（量程 **1–5**）为辅（权重 0.4）。聚合前必须归一化：`trajectory_norm=(score-1)/2`，`tool_call_norm=(score-1)/4`。Composite = `0.6×trajectory_norm + 0.4×tool_call_norm`。若只选一个：TrajectoryComprehensiveGrader 覆盖全链路质量。"

无 Skill 的回答看似合理，但遗漏了量程不匹配的问题——在生产环境中是一个静默 bug。

---

## 项目结构

```
skill-anything/
├── SKILL.md                    # 编排者工作流（Claude 读取此文件）
├── agents/
│   ├── researcher.md           # 研究员角色指南
│   ├── skill-writer.md         # 技能作者角色指南
│   ├── eval-designer.md        # 评估设计师角色指南
│   └── grader.md               # 评判官角色指南
├── scripts/                    # 纯标准库 Python 辅助脚本
│   ├── blind_eval.py
│   ├── deblind_and_score.py
│   ├── validate_skill.py
│   ├── gen_viewer.py
│   ├── register_skill.py
│   ├── generate_catalog.py
│   └── ...
├── references/
│   └── eval-loop.md            # 迭代循环详细规范
├── catalog-skill/
│   └── SKILL.md                # 指向已发布 catalog 的入口
├── registry.json               # 已发布 Skill 结构化索引
├── published/                  # 已发布的 Skill 文件
└── workspace/                  # 每次蒸馏运行时创建
    ├── orchestrator-state.json
    ├── knowledge/              # 研究员产出
    ├── skills/                 # 生成的 Skill 文件
    └── evals/                  # 评估任务与结果
```

---

## Skill 输出格式

Skill 遵循 [Anthropic SKILL.md 标准](https://docs.anthropic.com/en/docs/claude-code/skills)，兼容 27+ Agent 平台：

```yaml
---
name: my-skill
description: >-
  本 Skill 的功能及使用时机描述。（≤1024 字符）
metadata:
  author: skill-anything
  version: "1.0"
  sa-source-repo: "https://github.com/org/repo"
  sa-generated-at: "2026-04-14T00:00:00Z"
  sa-eval-score: "0.86"
---
# Skill 正文内容（< 500 行）
...
```

自定义元数据使用 `sa-` 前缀（非侵入式，标准兼容）。

---

## 发布 Skill

```bash
# 将蒸馏完成的 Skill 注册到 catalog
python scripts/register_skill.py workspace/ https://github.com/org/repo

# 重新生成 catalog
python scripts/generate_catalog.py

# 发布
git add published/ registry.json catalog-skill/
git commit -m "publish: repo-name skills"
git push
```

Agent 通过三级层次发现 Skill：指针（40 行）→ 目录索引（100 行）→ 完整 Skill（按需加载）。

---

## 设计原则

**Claude 即编排者，而非管线。** 复杂性存在于自然语言指令中，而非代码中。这意味着零构建链依赖，以及在所有兼容 Claude 的平台上行为一致。

**评估隔离。** 评估设计师在物理隔离目录中工作——它无法看到它将要测试的 Skill。评判官接收随机化的 A/B 标签。两项保证都是架构级的，而非仅依靠流程规范。

**简洁性偏好。** 用更少文字产出等效结果的 Skill 严格优于冗长版本。每轮迭代都可以在不影响评分的前提下删除内容——这是一个好结果，不是损失。

---

## 贡献指南

欢迎贡献。以下方向最需要帮助：

- **新目标仓库** — 提 issue 说明仓库 URL 及其作为蒸馏目标的价值
- **评估任务质量** — 区分度更高的评估任务
- **脚本改进** — 辅助脚本保持极简设计，优先接受保持标准库限制的 PR
- **平台兼容性测试** — 验证 Skill 在非 Claude Code Agent 上的兼容性

大型变更前请先提 issue。保持 PR 聚焦。

---

## 引用

如果你在研究中使用了 `skill-anything` 或基于本项目进行了后续工作，请引用：

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

## 许可证

版权所有 2026 vibe-researcher

本项目使用 [Apache License 2.0](LICENSE) 授权。未经许可不得违反 License 条款使用本项目。

---

## 致谢

- [Anthropic](https://anthropic.com) 提供了 SKILL.md 标准和 Claude Code 平台
- [OpenJudge](https://github.com/agentscope-ai/OpenJudge) 作为本 README 的主要案例仓库
- Agent 评估社区在盲法评估和轨迹评分方面的工作，为本系统的设计提供了灵感
