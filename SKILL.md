---
name: skill-anything
description: >-
  从 GitHub Repo 蒸馏出高质量 Agent Skill。输入 repo URL，自闭环完成
  知识研究、Skill 生成、对比评估与迭代优化。用于处理 Agent 频繁遇到
  但表现不佳的框架/工具库（如 Tailwind CSS v4、MCP SDK）。
metadata:
  author: skill-anything
  version: "0.4.0"
---

# skill-anything

## Instructions

你是知识蒸馏 Orchestrator。输入一个 GitHub repo URL，通过 研究 → 生成 → 评估 → 迭代 的循环，将 repo 中分散的、面向人类的知识蒸馏为 Agent 可直接使用的 Skill 文件。

整体流程：

- 研究 repo，产出 Knowledge Map
- 从 Knowledge Map 生成 Skill 文件
- 设计评估任务（与 Skill 内容隔离）
- 运行 with/without Skill 对比评估
- 盲法评判 → 反馈 → 改进 Skill
- 迭代直到收敛或用户满意

你的工作是**调度和决策**。用子代理完成重活（研究、写作、评判），自己只读取产出物、做全局判断。但你也应灵活应对——如果用户说"不用跑那么多轮评估，先帮我看看初版"，那就照做。

### 核心原则

- **上下文隔离**：Eval Designer 看不到 Skill，Judge 不知道哪个是 Skill 输出，Runner+S 和 Runner-S 独立运行
- **文件系统即状态**：通过 workspace 中文件的存在性判断进度，不需要额外状态机
- **简洁性准则**：删除后效果不降 = 胜利。同时适用于 Skill 内容和系统本身

### 如何调度子代理

当需要子代理完成某项工作时：

1. 读取对应的 `agents/<role>.md` 文件，了解该角色的职责和输出格式
2. Spawn 一个子代理（使用 Task tool 或其他可用的子代理机制），将 agents 文件内容 + 任务提示一起传入
3. 指定子代理的工作目录
4. 子代理完成后，读取其产出的文件，做下一步决策

每个子代理独立工作、独立上下文，完成后你只读取它写入的文件。

---

### 工作空间设置

首次运行时，初始化 workspace：

```bash
mkdir -p workspace/{knowledge,skills,evals/results,logs}
cd workspace
git init
echo "repo/\nlogs/" > .gitignore
git add -A && git commit -m "workspace initialized" --allow-empty
echo "iteration\tcomposite\tcost_usd\tstatus\tdescription" > results.tsv
```

---

### Stage 1: 研究 Repo

读取 `agents/researcher.md`，spawn Researcher 子代理。

子代理任务提示：

```
目标 repo: <url>
Clone repo 到 ./repo/（如未 clone），深度研究。
产出 knowledge/knowledge-map.yaml。
完成后执行自洽性检验。
```

工作目录：`workspace/`

完成后验证 `workspace/knowledge/knowledge-map.yaml` 存在且可解析。

**定向补研**：如果后续迭代中 Judge 反馈指出 Skill 缺少某方面知识，且 Knowledge Map 无对应域，再次 spawn Researcher 子代理做定向补研，指定具体要补充的知识点。

---

### Stage 2: 初始生成

#### 2a. 生成 Skill

读取 `agents/skill-writer.md`，spawn Skill Writer 子代理。

子代理任务提示（首次）：

```
读取 knowledge/knowledge-map.yaml。
设计 Skill 拆分方案（基于域的自足性），生成 Skill 文件到 skills/ 目录。
遵循 Anthropic SKILL.md 标准。
执行自洽性检验。
```

工作目录：`workspace/`

完成后验证 `workspace/skills/` 下至少有一个 SKILL.md。可选用验证脚本：

```bash
python scripts/validate_skill.py workspace/skills/<skill-name>
```

保存快照：

```bash
cd workspace && git add -A && git commit -m "initial skills" && git tag skill-v0
```

#### 2b. 设计评估任务（与 Skill 隔离）

**关键**：Eval Designer 不能看到 skills/ 目录。准备隔离环境：

```bash
TMPDIR=$(mktemp -d)
cp -r workspace/knowledge "$TMPDIR/knowledge"
```

读取 `agents/eval-designer.md`，spawn Eval Designer 子代理。

子代理任务提示：

```
读取 knowledge/knowledge-map.yaml。
设计有区分度的评估任务，写入 eval-tasks.json。
你无法访问任何 Skill 文件——这是故意的，不要尝试寻找。
执行自洽性检验。
```

工作目录：`$TMPDIR`（隔离目录）

完成后：

```bash
cp "$TMPDIR/eval-tasks.json" workspace/evals/eval-tasks.json
rm -rf "$TMPDIR"
```

---

### Stage 3: 迭代优化循环

每轮迭代按 a → b → c → d 顺序执行。

#### 3a. 运行对比评估

对每个 eval task，**同时** spawn 两个 Runner 子代理（尽量并行）：

**Runner+S（有 Skill）** — 准备临时目录并复制 skills/：

```bash
RUNDIR_WITH=$(mktemp -d)
cp -r workspace/skills "$RUNDIR_WITH/skills"
```

子代理任务提示：

```
你是一个开发者 Agent。
开始任务之前，先阅读 ./skills/ 目录下所有 SKILL.md 文件，将其中的指导作为知识基础。
然后完成任务：<task description>
```

工作目录：`$RUNDIR_WITH`

**Runner-S（无 Skill）** — 空临时目录：

```bash
RUNDIR_WITHOUT=$(mktemp -d)
```

子代理任务提示：

```
你是一个开发者 Agent。根据你已有的知识完成任务：<task description>
```

工作目录：`$RUNDIR_WITHOUT`

收集每个 Runner 的返回结果——最终输出文本、工具调用次数、创建的文件。将所有结果写入 `workspace/evals/results/iter-<N>/eval-results.json`：

```json
{
  "iteration": 1,
  "tasks": [
    {
      "taskId": "task-id",
      "withSkill": { "text": "...", "toolUseCount": 12, "toolUses": ["Read", "Write", ...], "createdFiles": [...] },
      "withoutSkill": { "text": "...", "toolUseCount": 34, "toolUses": [...], "createdFiles": [...] }
    }
  ]
}
```

#### 3b. 盲法评判

准备盲化数据：对每个 task，随机决定 A/B 标签分配。

```python
import random
# 对每个 task：
a_is_with = random.random() > 0.5
# 如果 a_is_with: Output A = withSkill, Output B = withoutSkill
# 否则反过来
```

将盲化后的数据写入 `workspace/evals/results/iter-<N>/blinded-eval-results.json`，同时将映射关系记在 `blind-mapping.json` 中。

读取 `agents/judge.md`，spawn Judge 子代理。

子代理任务提示：

```
盲法评估：对每个 task 的 Output A 和 Output B 进行质量评判。
你不知道哪个使用了 Skill——请纯粹基于输出质量评判。
读取 evals/eval-tasks.json 了解任务定义。
读取 evals/results/iter-<N>/blinded-eval-results.json 了解两个输出。
对每个任务评分，写入 evals/results/iter-<N>/blind-judge-scores.json。
执行自洽性检验。
```

工作目录：`workspace/`

Judge 完成后，**你来反盲化**：根据 `blind-mapping.json` 将 A/B 标签映射回 with/without，计算 `skillWon` (yes/no/tie)，写入 `judge-scores.json`。

反盲化后的 `judge-scores.json` 必须符合以下格式（viewer 和综合评分依赖此格式）：

```json
[
  {
    "taskId": "task-id",
    "channels": {
      "executionPass": true,
      "assertionCoverage": 0.85,
      "llmJudgeScore": 4.2,
      "trajectoryEfficiency": {
        "toolCallsWith": 12,
        "toolCallsWithout": 34
      }
    },
    "blindComparison": {
      "winner": "A",
      "skillWasOutput": "B",
      "skillWon": "yes",
      "reasoning": "Output A 完成了所有预期行为..."
    },
    "feedback": "获胜输出覆盖了核心 API 模式，但缺少错误处理...",
    "suggestion": "增加 Tool handler 中的错误处理模式",
    "evalFeedback": {
      "suggestions": [
        { "assertion": "断言内容", "reason": "该断言过于宽泛" }
      ],
      "repeatedWorkPatterns": ["两个输出都独立编写了 schema 验证辅助函数"]
    }
  }
]
```

字段映射规则：
- `channels` 取 **with-Skill 侧** 的评分（executionPass / assertionCoverage / llmJudgeScore 来自 Judge 对 Skill 输出的评分）
- `trajectoryEfficiency` 的 toolCallsWith / toolCallsWithout 来自 `eval-results.json` 中的 toolUseCount
- `blindComparison.skillWon`：winner 是 Skill 侧 → "yes"，winner 是 baseline → "no"，TIE → "tie"
- `feedback` / `suggestion` / `evalFeedback` 直接取自 Judge 输出

#### 3c. 计算综合评分并检查收敛

综合评分公式：

```
composite = executionPass×0.2 + assertionCoverage×0.25 + (llmJudgeScore/5)×0.35 + trajectory×0.2
其中 trajectory = max(0, 1 - toolCallsWith/toolCallsWithout)
```

取所有任务的 composite 均值。

用收敛脚本检查是否收敛：

```bash
python scripts/convergence.py workspace/ <composite-score> --cost <本轮花费USD>
```

根据输出决策：

- `converged: true` → 进入 Stage 4
- `reason: "budget_exhausted"` → 停止，输出当前最优
- `reason: "continuing"` → 继续 3d

#### 3d. 改进 Skill

**先判断是否有回归**：对比当前和上一轮每个 task 的得分，如果任何 task 下降 > 0.05：

```bash
cd workspace && git checkout skill-v<上一版> -- skills/
```

然后 spawn Skill Writer 时附带约束：`"REGRESSION on task-X. 修改必须解决反馈，同时不降低 task-X 得分。"`

无回归时，正常 spawn Skill Writer 子代理：

```
改进 Skills。读取：
1. knowledge/knowledge-map.yaml
2. skills/ — 当前 Skill
3. evals/results/iter-<N>/judge-scores.json — Judge 反馈
执行自洽性检验。
```

保存快照：

```bash
cd workspace && git add -A && git commit -m "iter-N: <描述>" && git tag skill-v<N>
```

**Eval 自举**（可选）：如果多个 task 的 with/without 差异 < 0.1，说明 eval 区分度不够。重新 spawn Eval Designer（隔离环境）替换弱任务，回到 3a。

**人类审查**（可选）：在需要人介入时（首次 eval set 审查、收敛后 spot check），生成 viewer：

```bash
python scripts/gen_viewer.py workspace/
```

自动在浏览器中打开交互式 HTML。用户点击 "Export Feedback JSON" 导出反馈文件。

---

### Stage 4: 产出

收敛后，向用户报告：

- 产出了几个 Skill（列出 `workspace/skills/` 目录内容）
- 最终评分和 with/without 差异
- 总迭代次数
- `workspace/results.tsv` 实验日志

最终 Skill 文件位于 `workspace/skills/`，可直接复制到用户的技能目录使用。

---

### 质量红线

以下决策由你直接做，不委托给子代理：

1. **回归检测**：score(t) < score(t-1) - 0.05 → 回滚 + 带约束重写
2. **粒度振荡**：连续 2 轮 split↔merge → 锁定粒度，只允许内容优化
3. **预算耗尽** → 停止，输出当前最优版本
4. **连续 3 轮无改善**（|Δs| < 0.02）→ 让 Skill Writer 从头重构，仍无效则判定收敛
5. **子代理失败** → 最多重试 2 次

### 评分公式参考

```
composite = executionPass×0.2 + assertionCoverage×0.25 + (llmJudgeScore/5)×0.35 + trajectory×0.2
trajectory = max(0, 1 - toolCallsWith/toolCallsWithout)
```

### 辅助脚本

| 脚本 | 用途 | 调用方式 |
|------|------|----------|
| `scripts/convergence.py` | 收敛判定 + results.tsv 追加 | `python scripts/convergence.py <workspace> <score> [--cost <usd>] [--status keep\|discard] [--desc "text"]` |
| `scripts/validate_skill.py` | 验证 Skill 格式合规性 | `python scripts/validate_skill.py <skill-dir>` |
| `scripts/gen_viewer.py` | 生成 eval viewer HTML | `python scripts/gen_viewer.py <workspace> [iteration]` |

## Additional Resources

- [Researcher 指南](agents/researcher.md) — 研究 repo、产出 Knowledge Map
- [Skill Writer 指南](agents/skill-writer.md) — Anthropic SKILL.md 标准、写作原则
- [Eval Designer 指南](agents/eval-designer.md) — 评估任务设计、区分度原则
- [Judge 指南](agents/judge.md) — 多通道评分、盲法评判流程
