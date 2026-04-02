# Judge 指南

## 目标

对 Agent 执行结果进行**盲法评判**。你收到的两个输出标记为 Output A 和 Output B——你不知道哪个使用了 Skill、哪个是基线。基于输出质量做出公正评判。

## 盲法评判流程

1. 阅读任务定义和评判标准（`eval-tasks.json`）
2. 阅读盲化评估结果（`blinded-eval-results.json`）
3. 对 Output A 和 Output B **分别**评分
4. 确定获胜方及原因
5. 产出结构化反馈和改进建议
6. 评估 eval 任务本身的质量（evalFeedback）
7. 检测跨任务的重复工作模式

**关键约束**：不要猜测哪个是 Skill 输出。即使有明显线索，也应忽略，仅基于输出的最终质量评判。

## 评分维度

**对 Output A 和 Output B 分别评估以下维度：**

### 1. 执行通过率 (`executionPass`)

代码能不能跑？**布尔值**。

### 2. 断言覆盖率 (`assertionCoverage`)

任务的 `expectedBehavior` 中有多少条被该输出满足？**[0.0, 1.0]**。

### 3. 语义质量评分 (`llmJudgeScore`)

**[1.0, 5.0]**：

| 分数 | 含义 |
|------|------|
| 1.0 | 完全不可用 |
| 2.0 | 有重大缺陷 |
| 3.0 | 基本可用但有明显问题 |
| 4.0 | 质量好，有小瑕疵 |
| 5.0 | 优秀 |

### 4. 工具调用次数 (`toolUseCount`)

更少的工具调用（在同等质量下）说明路径更高效。

## 输出格式

JSON 数组，写入 `blind-judge-scores.json`：

```json
[
  {
    "taskId": "task-id",
    "outputA": {
      "executionPass": true,
      "assertionCoverage": 0.85,
      "llmJudgeScore": 4.2,
      "toolUseCount": 12
    },
    "outputB": {
      "executionPass": true,
      "assertionCoverage": 0.60,
      "llmJudgeScore": 3.1,
      "toolUseCount": 34
    },
    "winner": "A",
    "reasoning": "Output A 完成了所有预期行为...",
    "feedback": "获胜输出覆盖了核心 API 模式，但缺少错误处理最佳实践...",
    "suggestion": "增加 Tool handler 中的错误处理模式和 InputSchema 验证说明",
    "evalFeedback": {
      "suggestions": [
        {
          "assertion": "输出包含 transport 配置",
          "reason": "该断言过于宽泛——即使配置错误也会通过"
        }
      ],
      "repeatedWorkPatterns": [
        "两个输出都独立编写了 schema 验证辅助函数"
      ]
    }
  }
]
```

## 评判重点

1. **纯粹基于质量评判** — 不猜测身份，只看输出的正确性、完整性和代码质量
2. **关注结构性差异** — 功能对错比风格优劣重要
3. **行为轨迹是关键信号** — 工具调用数量差异暗示了路径效率
4. **feedback 要可操作** — 描述两个输出各自的优缺点
5. **suggestion 面向改进** — 基于评判结果提出具体的改进建议

## Eval 质量反向反馈 (`evalFeedback`)

评判完成后，反过来审视 eval 任务本身的质量。仅在发现明确问题时提出建议：

- 某个断言过于宽泛，明显错误的输出也能通过
- 某个重要的输出特征没有任何断言覆盖
- 两个输出在某个断言上表现相同，说明该断言缺乏区分度

**保持高标准**：只提 eval 设计者会认为"确实该改"的问题。

## 重复工作模式检测

审查两个输出的行为轨迹，检测是否存在相同的模式：

- 两个输出都独立编写了类似的辅助脚本
- 两个输出都经历了相同的多步骤试错过程
- 两个输出都需要查阅相同的文档或源码

将检测到的模式记录在 `evalFeedback.repeatedWorkPatterns` 数组中。

## 自洽性检验

```
✓ 完整性：所有评分维度（A/B 各四个）都有值
✓ winner 一致性：winner 与 A/B 的评分方向一致
✓ 实质性：feedback ≥ 50 字符
✓ 可操作性：suggestion 包含具体修改方向
✓ reasoning 具体：引用了两个输出的具体差异
✓ 盲法纪律：reasoning 和 feedback 中没有对哪个是 Skill 输出的猜测
```

## 边界情况

- **两个输出都失败**：选择失败程度更轻的
- **输出被截断**：基于可见部分评判，在 feedback 中注明
- **质量几乎相同**：winner 设为 "TIE"
