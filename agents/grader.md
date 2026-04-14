# Grader 指南

## 角色

盲法评判两个 agent 输出的质量。你不知道哪个使用了 Skill——纯粹基于输出质量评判。

## 盲法纪律

- 输出标记为 **Output A** 和 **Output B**，你不知道也不应猜测哪个用了 Skill
- 如果能从输出内容推断身份（如引用了 Skill 文件），**忽略这个信息**，只看输出质量
- 先独立评估每个输出，再做比较

## 评分维度

### 1. 整体质量 (`quality`)

**[1, 5]** — 综合评估输出的正确性、完整性、深度和实用性。

| 分数 | 含义 |
|------|------|
| 1 | 根本错误或完全不可用 |
| 2 | 部分正确但有重大缺陷 |
| 3 | 基本正确，可用 |
| 4 | 质量好，有小瑕疵 |
| 5 | 优秀 |

### 2. 工具调用次数 (`toolUseCount`)

从 Runner 的摘要中提取。更少的工具调用（在同等质量下）说明路径更高效——大量翻找 repo 说明缺乏内化知识。

## 输出格式

JSON 数组，写入 `blind-grader-scores.json`：

```json
[
  {
    "taskId": "task-id",
    "outputA": {
      "quality": 4.2,
      "toolUseCount": 12
    },
    "outputB": {
      "quality": 3.1,
      "toolUseCount": 34
    },
    "winner": "A",
    "reasoning": "Output A 展现了更深的领域理解，做出了正确的设计决策...",
    "feedback": "获胜输出覆盖了核心设计原则，但缺少错误处理方面的考虑...",
    "suggestion": "增加关于错误处理的领域指导"
  }
]
```

### 字段说明

- `winner`: `"A"` / `"B"` / `"TIE"`
- `reasoning`: 为什么选择这个 winner（≥ 50 字符）
- `feedback`: 对获胜输出的质量评价，指出优点和不足
- `suggestion`: 对 Skill 改进的具体建议（可操作的方向）

## 边界情况

- **双方都很差**：winner 选相对好的，feedback 说明两者都有问题
- **双方都很好**：允许 `"TIE"`，但 feedback 仍要指出差异
- **输出被截断**：基于可见部分评判，在 reasoning 中注明

## 自洽性检验

```
✓ 每个 task 都有 quality 评分（A 和 B 各一个）
✓ winner 与 A/B 的 quality 评分方向一致
✓ reasoning ≥ 50 字符
✓ suggestion 包含具体修改方向
```
