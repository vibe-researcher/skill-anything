# 迭代循环（Eval Loop）

每轮按 Run → Blind → Judge → Score → Improve 顺序执行。

## Run: 运行对比评估

对每个 eval task，**同时** spawn 两个 Runner 子代理（尽量并行）。

**关键设计**：两个 Runner 都拥有 repo/ 副本。Eval 测量的是"蒸馏态 Skill vs 原始态 repo"，而非"有文档 vs 无文档"。

**Runner+S（有 Skill）** — repo + skills：

```bash
RUNDIR_WITH=$(mktemp -d)
cp -r workspace/repo "$RUNDIR_WITH/repo"
cp -r workspace/skills "$RUNDIR_WITH/skills"
```

子代理任务提示：

```
你是一个开发者 Agent。
开始任务之前，先阅读 ./skills/ 目录下所有 SKILL.md 文件，将其中的指导作为知识基础。
然后完成任务：<task description>
./repo/ 中有项目源码可供参考，但 Skill 应已涵盖你需要的核心知识——优先依赖 Skill 中的指导。
完成后将所有产出写入文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
TOOL_CALLS: <工具调用总次数>
```

工作目录：`$RUNDIR_WITH`

**Runner-S（无 Skill）** — 仅 repo：

```bash
RUNDIR_WITHOUT=$(mktemp -d)
cp -r workspace/repo "$RUNDIR_WITHOUT/repo"
```

子代理任务提示：

```
你是一个开发者 Agent。./repo/ 中有项目源码可供参考。
根据你的知识和 repo 中的信息完成任务：<task description>
完成后将所有产出写入文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
TOOL_CALLS: <工具调用总次数>
```

工作目录：`$RUNDIR_WITHOUT`

从每个 Runner 的返回摘要中提取 `TOOL_CALLS` 数值和 `STATUS`。将结果写入 `workspace/evals/results/iter-<N>/eval-results.json`：

```json
{
  "iteration": 1,
  "tasks": [
    {
      "taskId": "task-id",
      "withSkill": { "text": "...", "toolUseCount": 12, "toolUses": ["Read", "Write", "..."], "createdFiles": ["..."] },
      "withoutSkill": { "text": "...", "toolUseCount": 34, "toolUses": ["..."], "createdFiles": ["..."] }
    }
  ]
}
```

**注意**：`text` 字段应简短（来自 Runner 摘要），不要将 Runner 的完整对话历史写入。

## Blind: 盲化（脚本自动完成）

```bash
python scripts/blind_eval.py workspace/ <N>
```

自动完成：
- 随机 A/B 标签分配
- 写入 `blinded-eval-results.json`
- 写入 `blind-mapping.json`（不要读取此文件——你不应知道映射关系）

## Judge: 盲法评判

读取 `agents/judge.md`，spawn Judge 子代理。

子代理任务提示：

```
盲法评估：对每个 task 的 Output A 和 Output B 进行质量评判。
你不知道哪个使用了 Skill——请纯粹基于输出质量评判。
读取 evals/eval-tasks.json 了解任务定义。
读取 evals/results/iter-<N>/blinded-eval-results.json 了解两个输出。
对每个任务评分，写入 evals/results/iter-<N>/blind-judge-scores.json。
执行自洽性检验。
完成后将所有产出写入指定文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
```

工作目录：`workspace/`

`blind-judge-scores.json` 格式（Judge 产出，完整规范见 `agents/judge.md`）：

```json
[
  {
    "taskId": "task-id",
    "taskType": "coding",
    "outputA": {
      "executionPass": true,
      "assertionCoverage": 0.85,
      "llmJudgeScore": 4.2,
      "novelApplicability": null,
      "toolUseCount": 12
    },
    "outputB": {
      "executionPass": true,
      "assertionCoverage": 0.60,
      "llmJudgeScore": 3.1,
      "novelApplicability": null,
      "toolUseCount": 34
    },
    "winner": "A",
    "reasoning": "Output A 完成了所有预期行为...",
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

对 reasoning/transfer task，`executionPass` 和 `assertionCoverage` 为 `null`，`novelApplicability` 为 `[1-5]`。

## Score: 反盲化 + 评分 + 收敛检查（脚本自动完成）

```bash
python scripts/deblind_and_score.py workspace/ <N> --cost <本轮花费USD>
```

脚本自动完成以下全部工作：
1. 读取 `blind-mapping.json` 反盲化
2. 用 with-Skill 侧的评分计算 composite
3. 检测回归（与上一轮对比）
4. 调用 `convergence.py` 判断收敛
5. 写入 `judge-scores.json`（完整格式，供 viewer 使用）
6. 写入 `iteration-summary.json`（紧凑摘要，供 Orchestrator 使用）

**stdout 输出**（3-4 行，这是你唯一需要读的）：

```
composite=0.72 converged=false reason=continuing
weakest: task-2, task-5
details: workspace/evals/results/iter-1/iteration-summary.json
```

如有回归，额外输出一行 `REGRESSIONS: task-3`。

### 评分公式

对 coding 类型 task：

```
composite = executionPass×0.1 + assertionCoverage×0.15 + (llmJudgeScore/5)×0.40 + trajectory×0.35
trajectory = max(0, 1 - toolCallsWith/toolCallsWithout)
```

对 reasoning / transfer 类型 task（无 executionPass / assertionCoverage）：

```
composite = (llmJudgeScore/5)×0.55 + trajectory×0.45
```

设计理由：两个 Runner 都有 repo 访问，trajectory 差异真正反映蒸馏效率——Runner-S 需要大量 Read/Grep 搜索 repo，Runner+S 读完 Skill 后应能直接行动。

### `judge-scores.json` 格式（脚本产出）

```json
[
  {
    "taskId": "task-id",
    "composite": 0.72,
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
    "feedback": "...",
    "suggestion": "...",
    "evalFeedback": { "..." }
  }
]
```

### `iteration-summary.json` 格式（Orchestrator 应读此文件）

```json
{
  "iteration": 1,
  "composite_score": 0.72,
  "knowledge_density": 0.48,
  "skill_line_count": 150,
  "converged": false,
  "convergence_reason": "continuing",
  "regressions": [],
  "weakest_tasks": ["task-2", "task-5"],
  "per_task": [
    {
      "taskId": "task-1",
      "composite": 0.85,
      "skillWon": "yes",
      "delta": 0.05,
      "feedback": "覆盖了核心 API...",
      "suggestion": "增加错误处理..."
    }
  ]
}
```

`knowledge_density = composite_score / skill_line_count × 100`：同等评分下，行数更少的 Skill 密度更高。如果迭代中 density 下降（评分没涨但行数增加），提示 Skill Writer 精简。

## Improve: 改进 Skill

**基于 `iteration-summary.json` 决策**，不要读 `judge-scores.json`：

1. 如果有 `regressions`：回滚 skills 并带约束重写

```bash
cd workspace && git checkout skill-v<上一版> -- skills/
```

spawn Skill Writer 时附带约束：`"REGRESSION on task-X. 修改必须解决反馈，同时不降低 task-X 得分。"`

2. 无回归时，正常 spawn Skill Writer：

```
改进 Skills。读取：
1. knowledge/knowledge-map.yaml
2. skills/ — 当前 Skill
3. evals/results/iter-<N>/iteration-summary.json — 本轮评估摘要（含 per-task feedback 和 suggestion）
执行自洽性检验。
完成后将所有产出写入指定文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
```

保存快照：

```bash
cd workspace && git add -A && git commit -m "iter-N: <描述>" && git tag skill-v<N>
```

**Eval 自举**（可选）：如果多个 task 的 with/without 差异 < 0.1，说明 eval 区分度不够。重新 spawn Eval Designer（隔离环境）替换弱任务，回到 Run。

**人类审查**（可选）：生成 viewer：

```bash
python scripts/gen_viewer.py workspace/
```
