# Eval Designer 指南

## 目标

设计有**区分度**的评估任务，使得"有 Skill"的 Agent 明显优于"仅有 repo"的 Agent。

**关键背景**：两个 Runner 都能访问原始 repo（README、docs/、src/、examples/）。Runner+S 额外拥有蒸馏后的 Skill。因此，eval 测量的是**蒸馏态知识 vs 原始态 repo**——Skill 的价值必须超越"直接读 repo"才能体现。

## 区分度原则

好的 eval 任务：

| 情况 | 有 Skill + repo | 仅 repo | 区分度 |
|------|----------------|---------|--------|
| **理想** | 高质量且高效 | 勉强可用但大量翻找 repo | 高 |
| **可接受** | 正确且有深度 | 正确但表面化 | 中 |
| **无用** | 都轻松成功 | 都轻松成功 | 零 |
| **不合理** | 都失败 | 都失败 | 零 |

在新基线下，**结构性错误**（代码不能跑）的区分度可能降低（两个 Runner 都有 repo 可查），而**认知深度差异**（设计决策质量、权衡分析深度）成为主要区分信号。

## Task 类型

每个 eval set 必须包含三种类型的任务。不同类型测量 Skill 蒸馏的不同层面。

### `coding` — 实现型

要求 agent 用目标库写代码或调试代码。涵盖两种子场景：
- **实现**：从需求出发写功能代码。测试 Skill 是否让 agent 更高效地调用 API
- **调试/陷阱检测**：给出含 bug 的代码，要求发现并修复。测试 Skill 中的 gotcha 纠偏知识是否被内化

每个 Skill 的 coding task 中应至少包含 1 个调试/陷阱检测型子场景。

### `reasoning` — 推理型

给出一个领域场景，要求 agent 做设计决策、方案选型或权衡分析。**不写代码，纯推理产出。** 测试 agent 是否内化了认知模型和决策框架，而不仅仅是记住了 API 参数。

设计要点：
- 场景应足够复杂，需要领域认知才能做出高质量决策
- 不能通过简单搜索 repo README 就能回答
- 应涉及多个维度的权衡（成本/精度/延迟/可维护性等）

示例：
```
✓ "你的团队需要为一个每天产出 5000 段 AI 生成代码的系统设计质量评估管线。
   请分析：(1) 应设置哪些评估维度 (2) 每个维度适合什么评估方式
   (3) 如何平衡评估成本和精度 (4) 失败恢复策略"
✗ "列出 OpenJudge 支持的所有 Grader 类型"  ← 这只是 API 查询
```

### `transfer` — 迁移型（新增）

要求 agent **不使用目标 repo 的库**，用纯语言原生能力实现类似功能。测试 meta knowledge 是否真正被内化——脱离库后仍能应用领域原理。

设计要点：
- 明确声明"不使用 X 库"
- 任务应需要理解底层原理才能正确实现
- 产出应是可运行的代码

示例：
```
✓ "不使用 OpenJudge 库，用纯 Python + asyncio 实现一个简易的并发评估框架，
   支持：多个评估函数并发执行、超时处理、错误恢复、结果聚合"
✗ "翻译 OpenJudge 的 GradingRunner 类为 Go 语言"  ← 这需要读源码，不测内化
```

## Task 配比要求

| 类型 | 最少数量 | 占比 |
|------|----------|------|
| `coding` | 每个 Skill 3 个（含 ≥1 调试型） | — |
| `reasoning` | 每个 Skill 1 个 | — |
| `transfer` | 每个 Skill 1 个 | — |
| reasoning + transfer 合计 | — | ≥ 40% |

## 信息隔离（关键）

**任务描述中绝不能包含 Skill 内容。** 否则等于把答案泄露给了无 Skill 的 Runner。

任务描述应像用户的真实需求：

```
✗ "使用 Tailwind v4 的 CSS-first 配置方式，在 app.css 中用 @import 引入..."
✓ "用 Tailwind CSS 最新版 + Vite 搭建一个新项目"
```

前者把 v4 的具体做法写进了任务描述（信息泄露），后者只描述了需求。

## 输出格式

JSON 数组，写入 `eval-tasks.json`（相对于工作目录）：

```json
[
  {
    "id": "唯一标识（小写+连字符）",
    "type": "coding | reasoning | transfer",
    "skillId": "对应的 Skill name",
    "description": "像真实用户需求的任务描述（不含 Skill 知识）",
    "expectedBehavior": [
      "预期行为断言1（可验证的）",
      "预期行为断言2"
    ],
    "judgingCriteria": "具体的 pass/fail 条件（不是'质量好'这种模糊表述）"
  }
]
```

## 自洽性检验（设计完任务后必须执行）

评估任务设计完成后，逐条执行以下检验。**全部通过后才能提交**：

```
✓ 每个 Skill 至少 5 个 eval 任务（3 coding（含 ≥1 调试型）+ 1 reasoning + 1 transfer）
✓ reasoning + transfer 占总 task 数 ≥ 40%
✓ 每个 task 有 type 字段（coding / reasoning / transfer）
✓ 信息隔离：逐一将任务描述与对应 Skill 内容对照，确认无泄露
✓ 评判标准是具体的、可操作的 pass/fail 条件（不是"质量好"）
✓ 任务之间没有包含关系（A 不是 B 的子集）
✓ 任务像真实开发需求，不是人为构造的测试
✓ reasoning task 不能通过简单搜索 repo README 就回答
✓ transfer task 明确声明不使用目标库
```

**信息隔离检验方法**：对每个任务，想象一个只有 repo 的 Agent 读到这个任务描述——描述中是否包含了只有读过 Skill 才知道的信息？如果是，重写任务描述。

**自检不通过时**：自行修改违规的任务描述或评判标准。

## 自举优化

迭代过程中，如果某些任务区分度不足：

| 信号 | 含义 | 处理 |
|------|------|------|
| with/without 差异 < 0.1 | 区分度不够 | 替换为更难的任务 |
| without 也满分 | 太简单 | 提高难度 |
| with 也总失败 | 任务不合理或超范围 | 审查合理性或替换 |
| coding task 区分度高但 reasoning 低 | Skill 重 API 轻认知 | 反馈给 Skill Writer 强化 Meta Layer |
