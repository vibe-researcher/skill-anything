# OpenJudge Grader 架构与选型决策

## 核心抽象：评估即函数

OpenJudge 的根本抽象非常简单：一个 grader 就是一个异步函数，接收若干 kwargs，返回一个带 `score`（或 `rank`）和 `reason` 的结构化对象。所有复杂性都在这个函数内部。

这个简洁性背后有一个重要设计决策：**grader 不知道数据集，也不知道其他 grader**。它只处理单条样本（pointwise）或一组候选（listwise）。多 grader 编排、数据集迭代、并发控制全部交给 `GradingRunner`。

---

## 基础类型系统

### 三种返回类型

```python
GraderScore  # pointwise 评分，score: float + reason: str + metadata: dict
GraderRank   # listwise 排名，rank: List[int] + reason: str + metadata: dict
GraderError  # 评估失败，error: str + reason: str
```

`GraderRank` 的 `rank` 字段是**位置数组而非索引**。`rank=[1,3,2]` 表示：第1个候选排第1，第2个候选排第3，第3个候选排第2。系统会校验它必须是 `[1..n]` 的完整排列，任何重复或缺失都会在构造时报 `ValueError`。

### 两种模式

- **POINTWISE**：对每个样本独立打分，返回 `GraderScore`
- **LISTWISE**：对多个候选联合排序，返回 `GraderRank`

模式在 grader 实例化时固定，不能在调用时切换。

### score 的语义不统一

这是最容易踩坑的地方。**不同 grader 的 score 范围完全不同**：

| 类型 | 典型范围 | 说明 |
|------|---------|------|
| common/agent/multi_turn 系列 LLM grader | 1–5 | 5点 Likert 量表 |
| StringMatchGrader / MathExpressionVerifyGrader | 0.0–1.0 | 布尔或比例 |
| TextSimilarityGrader | 0.0–1.0 | 依算法而定 |
| ReasoningFormatGrader | 0.0 或 1.0 | 纯二元 |
| CodeExecutionGrader | 0.0–1.0 | 测试通过率 |

**没有全局归一化**。如果你把多个 grader 的 score 直接平均，会得到数学上毫无意义的结果。需要在聚合层显式处理量纲。

---

## 四大 Grader 基类

### 1. FunctionGrader —— "我自己写逻辑"

适用场景：你有一段确定性的评估逻辑，不需要 LLM。

```python
FunctionGrader(func=my_eval_fn, mode=GraderMode.POINTWISE)
# 或装饰器写法
@FunctionGrader.wrap
def my_eval_fn(query, response, **kwargs) -> GraderScore: ...
```

FunctionGrader 支持同步和异步函数，同步函数会被包装到线程池执行。它只做类型检查（返回值必须匹配 mode），其余全部委托给你的函数。

**典型用途**：exact match、规则 check、测试执行、数学符号验证（后两者其实也有专用子类）。

### 2. LLMGrader —— "让 LLM 打分"

适用场景：需要语义理解、无法用规则表达的评估维度。

LLMGrader 需要三个核心参数：
- `model`：`BaseChatModel` 实例或 dict（自动构造 `OpenAIChatModel`）
- `template`：prompt 模板，支持 str/list/dict/PromptTemplate 四种格式
- `mode`：POINTWISE 还是 LISTWISE

LLMGrader 通过 structured output（Pydantic 模型）强制 LLM 输出 JSON，然后解析为 `GraderScore` 或 `GraderRank`。

**关键行为**：template 中的变量（如 `{query}`, `{response}`）在 `aevaluate()` 调用时按 kwargs 填充。如果 template 里有 `{query}` 但你调用时没传 `query=...`，会报 `KeyError`，而不是 None 或空字符串。

### 3. AgenticGrader —— "让 Agent 自主评估"

适用场景：评估本身需要外部信息（搜索、代码执行、数据库查询）；或者评估逻辑复杂到需要多步推理。

AgenticGrader 的设计哲学是"unified interface"：它只接受一个已构建好的 `agent` 对象，不关心 agent 内部是 ReActAgent、LangChain 还是 AgentScope。

```python
agent = ReActAgent(model=..., tools=[WebSearchTool()], max_iterations=10)
grader = AgenticGrader(agent=agent, template="Evaluate: {response}")
```

AgenticGrader 用两种策略解析 agent 输出：
1. 正则提取 JSON（支持嵌套）
2. Fallback 到关键词 regex 提取 score/rank

如果两种都失败，抛 `ValueError`。这意味着你的 template 必须明确要求 agent 输出包含 `score` 或 `rank` 字段的 JSON。

**开销警告**：AgenticGrader 的每次调用涉及多轮 LLM 交互，成本可能是 LLMGrader 的 5–20 倍。只有当评估本身确实需要"工具调用"才用它。

### 4. BaseGrader（直接子类）—— "有特殊执行逻辑"

直接继承 BaseGrader 并实现 `_aevaluate`，适用于：
- 需要代码执行（`CodeExecutionGrader`）
- 需要专用库（`MathExpressionVerifyGrader` 使用 `math_verify`）
- 性能敏感的字符串/文本匹配（`StringMatchGrader`, `TextSimilarityGrader`）

---

## 预制 Grader 分类体系

### common/：与任务类型无关的通用维度

| Grader | 核心问题 | 输入 |
|--------|---------|------|
| `CorrectnessGrader` | 是否符合参考答案？ | query + response + reference_response |
| `HallucinationGrader` | 是否捏造了信息？ | query + response + context（可选）|
| `RelevanceGrader` | 回答是否切题？ | query + response |
| `HarmfulnessGrader` | 是否有害？ | query + response |
| `InstructionFollowingGrader` | 是否遵循了指令？ | query + response |

这些是最常用的 grader。注意 `CorrectnessGrader` 和 `HallucinationGrader` 的区别：Correctness 比较的是与**参考答案**的一致性（有 ground truth），Hallucination 检测的是**凭空捏造**（可以无 context）。

### agent/：评估 agent 行为轨迹

子目录按 agent 能力维度划分：

- `tool/`：工具调用质量（accuracy, precision/recall, step sequence, success, parameter check, selection）
- `trajectory/`：多步轨迹整体评估（accuracy, comprehensive）
- `action/`：动作合理性（alignment, loop detection）
- `reflection/`：反思质量（accuracy, outcome understanding, progress awareness）
- `memory/`：记忆能力（accuracy, detail preservation, retrieval effectiveness）
- `observation/`：信息增益
- `plan/`：计划可行性

agent/ grader 的输入通常是**消息列表**（OpenAI 格式的 conversation history），而非单条 response。工具调用信息藏在 `message["tool_calls"]` 字段中。

### multi_turn/：多轮对话特有问题

| Grader | 评估什么 |
|--------|---------|
| `ContextMemoryGrader` | 是否记住了早期轮次的信息 |
| `TopicSwitchGrader` | 话题切换时是否处理得当 |
| `SelfCorrectionGrader` | 是否能发现并纠正自己的错误 |
| `ResponseRepetitionGrader` | 是否重复了之前的回复 |
| `AnaphoraResolutionGrader` | 代词指代消解 |
| `InstructionClarificationGrader` | 模糊指令时是否主动澄清 |
| `ProactiveInteractionGrader` | 是否主动推进对话 |

这些 grader 都需要完整的对话历史（`history` 参数），而不是单条 response。

### multimodal/：需要视觉理解

- `ImageCoherenceGrader`：图文连贯性（图片与周围文本是否语义一致）
- `ImageHelpfulnessGrader`：图片是否有助于理解文本
- `TextToImageGrader`：文生图质量评估

Multimodal grader 内部将图片以 base64 编码注入 prompt，要求 model 支持视觉输入（如 GPT-4V、Qwen-VL）。

### code/：代码质量

- `CodeExecutionGrader`：运行测试用例，score = 通过率（0–1）
- `CodeBugDetectionGrader`：LLM 分析潜在 bug（1–5）
- `SyntaxCheckerGrader`：语法正确性
- `CodeSecurityGrader`：安全漏洞
- `CodeStyleGrader`：代码风格
- `CodeComplexityGrader`：复杂度评估
- `PatchSimilarityGrader`：与参考 patch 的相似度

### math/：数学表达式

- `MathExpressionVerifyGrader`：用 `math_verify` 库验证数学等价性（支持 LaTeX），score 为 0/1。

### text/：文本相似度

- `StringMatchGrader`：exact/prefix/suffix/regex/substring/contains_all/contains_any/word_overlap/char_overlap（算法在调用时通过 `algorithm=` 参数指定，不是在初始化时）
- `TextSimilarityGrader`：BLEU/ROUGE/METEOR/F1/cosine/jaccard 等

### format/：格式检查

- `ReasoningFormatGrader`：检查 `<think>...</think><answer>...</answer>` 标签结构
- `ReasoningToolFormatGrader`：工具调用格式
- `LengthPenaltyGrader`：长度惩罚
- `NGramRepetitionPenaltyGrader`：n-gram 重复惩罚
- `JsonMatchGrader` / `JsonValidatorGrader`：JSON 格式校验

### skills/：AI Skill 包质量评估

专为评估 AI Skill 设计，维度：Relevance、Completeness、Safety、Structure。有 pointwise（`SkillComprehensiveGrader`）和 pairwise（`SkillComprehensivePairwiseGrader`）两种变体。

---

## 选型决策框架

### 第一步：确认评估维度的性质

```
你的评估维度能被确定性规则表达吗？
  → 是：FunctionGrader 或专用确定性 grader（StringMatch, Math, Code）
  
  → 否：需要 LLM 语义理解
      → 评估本身需要查外部信息/工具？
          → 是：AgenticGrader
          → 否：LLMGrader（或其预制子类）
```

### 第二步：确认输入数据结构

| 你有什么 | 对应 grader 族 |
|---------|--------------|
| (query, response, ground_truth) | common/Correctness |
| (query, response) + 无 ground truth | common/Hallucination, Relevance |
| 完整对话历史（多轮） | multi_turn/ |
| Agent 工具调用轨迹（messages list） | agent/ |
| 图片 + 文本 | multimodal/ |
| 代码字符串 + 测试用例 | code/CodeExecution |
| 数学表达式 | math/MathExpressionVerify |
| 两个候选要比较优劣 | LISTWISE mode 任意 grader |

### 第三步：score 语义对齐

如果要组合多个 grader 的分数，必须明确各 grader 的 score 范围，并显式归一化。常见做法：

```python
# 1-5 量表归一化到 0-1
normalized = (score - 1) / 4

# 或者使用 AverageEvaluationStrategy 在同一 grader 内部多次采样
strategy = AverageEvaluationStrategy(num_evaluations=3)
grader = CorrectnessGrader(model=model, strategy=strategy)
```

### 第四步：选择评估策略（可选）

| 策略 | 用途 |
|------|------|
| `DirectEvaluationStrategy`（默认） | 单次评估，最快 |
| `VotingEvaluationStrategy(num_votes=5)` | 多数投票，适合二元/低粒度评估，减少 LLM 随机性 |
| `AverageEvaluationStrategy(num_evaluations=3)` | 平均分，适合连续量表评估 |

Voting 适合分类型输出（如 1-5 整数），Average 适合连续值。两者都会增加 LLM 调用次数，成本乘以 N。

---

## 组合多个 Grader 的逻辑

### 什么时候组合

- **覆盖不同维度**：correctness + hallucination + relevance = 全面 QA 评估
- **互补视角**：code execution（能不能跑）+ code bug detection（有没有潜在问题）
- **交叉验证**：同一维度用不同方法（rule-based + LLM），两者不一致时人工复核

### 什么时候不需要组合

- 已有 `SkillComprehensiveGrader` 这类"一次调用多维度"的 grader，不需要分开调用 Relevance/Completeness/Safety/Structure 再合并
- 任务单一，一个维度就够了（e.g., 纯数学题用 `MathExpressionVerifyGrader` 即可）

### GradingRunner 是组合的基础设施

```python
runner = GradingRunner(
    grader_configs={
        "correctness": correctness_grader,
        "hallucination": (hallucination_grader, {"q": "query", "a": "response"}),
    },
    max_concurrency=32,
)
results = await runner.arun(dataset)
```

`mapper` 参数解决字段名不匹配问题：数据集字段 `q` 映射到 grader 期待的 `query`。多个 grader 并发执行，结果按 grader name 和 sample index 组织。

---

## 反直觉陷阱

### 陷阱 1：score 范围无法直接比较

LLM grader 通常输出 1–5，text similarity 输出 0–1。合并时必须归一化，但框架不会自动做这件事。已经看到的 bug 模式：用 `(score_1 + score_2) / 2` 直接平均 1-5 量表和 0-1 比例值。

### 陷阱 2：AgenticGrader 不等于"更好的 LLM grader"

AgenticGrader 的价值在于它能**访问外部信息**（搜索、执行代码、查数据库）。如果评估维度只需要语义理解，用 AgenticGrader 只是更贵、更慢，且 parsing 更脆弱（依赖正则从自由文本提取 score）。

### 陷阱 3：LISTWISE 模式需要 prompt 传入所有候选

LLMGrader 的 LISTWISE 模式不会自动传入多个 response。你的 template 需要显式包含 `{answer_1}`、`{answer_2}` 等变量，调用时也要对应传参。`GraderRank` 的长度必须等于候选数量。

### 陷阱 4：multi_turn grader 不接受单条 response

`ContextMemoryGrader`、`TopicSwitchGrader` 等需要完整的 `history`（对话历史列表）。把它们用于单轮评估会得到无意义的分数（甚至直接失败），因为模板假设存在多个轮次。

### 陷阱 5：agent/ grader 与 AgenticGrader 是两件事

- `agent/` 目录下的 grader（如 `ToolCallAccuracyGrader`）：评估**被评估对象**（某个 agent）的行为，它们本身是 LLMGrader 子类，用 LLM 来做评估
- `AgenticGrader`：评估器本身是一个 agent，使用工具来完成评估任务

混淆这两个概念会导致完全错误的设计。

### 陷阱 6：StringMatchGrader 的算法在调用时指定，不在初始化时

```python
grader = StringMatchGrader()  # 初始化时不指定算法
result = await grader.aevaluate(
    reference_response="hello",
    response="hello world",
    algorithm="substring_match",  # 在 aevaluate 时指定
)
```

如果你以为初始化时就固定了算法，直接调用 `aevaluate` 不传 `algorithm`，会报参数错误。

### 陷阱 7：threshold 参数不影响 score，只是元数据

`CorrectnessGrader(threshold=3)` 的 threshold 会被塞进 `metadata["threshold"]`，但不会让 score<3 的结果变成 0，也不会触发任何过滤。如果你需要基于 threshold 过滤，需要在调用层自己判断 `result.score >= grader.threshold`。

### 陷阱 8：LLMGrader 复制状态到每次调用

当 grader 配置了 `strategy` 时，`aevaluate` 会 `deepcopy` 整个 grader 实例（包括 model）。这对于内存大的 model 对象（本地模型）可能造成意外开销。不配置 strategy 则跳过 deepcopy。

---

## 速查决策树

```
需要评估什么？
├── 数学答案 → MathExpressionVerifyGrader（0/1，支持LaTeX）
├── 代码能否运行 → CodeExecutionGrader（0-1，需要test_cases）
├── 代码有无 bug → CodeBugDetectionGrader（1-5，LLM判断）
├── 字符串匹配 → StringMatchGrader（指定algorithm）
├── 文本相似度 → TextSimilarityGrader（指定algorithm: BLEU/ROUGE/...）
├── 格式是否正确 → format/ 系列（0或1）
│
├── 语义维度（需要LLM）
│   ├── 与参考答案一致性 → CorrectnessGrader（1-5）
│   ├── 是否幻觉 → HallucinationGrader（1-5）
│   ├── 切题性 → RelevanceGrader（1-5）
│   ├── 有无害处 → HarmfulnessGrader（1-5）
│   ├── 遵循指令 → InstructionFollowingGrader（1-5）
│   └── 自定义维度 → LLMGrader（自定义 template）
│
├── 多轮对话特有问题 → multi_turn/ 系列（需要 history 列表）
├── Agent 工具调用质量 → agent/tool/ 系列（需要 messages with tool_calls）
├── Agent 完整轨迹 → agent/trajectory/ 系列
├── 图文相关性 → multimodal/ 系列（需要视觉模型）
├── AI Skill 包质量 → skills/ 系列
│
└── 评估需要查外部信息 → AgenticGrader（配置 tools）
```
