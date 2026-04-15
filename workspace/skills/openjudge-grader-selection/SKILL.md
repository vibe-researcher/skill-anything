---
name: openjudge-grader-selection
description: >-
  OpenJudge grader 选型与组合决策指南。当需要为 LLM/Agent 评估任务选择合适的 grader 类型、理解 score 语义差异、
  设计多维度评估方案时使用。覆盖四大基类、预制 grader 分类体系、选型决策树和组合陷阱。
metadata:
  author: skill-anything
  version: "1.0"
  sa-source-repo: "https://github.com/agentscope-ai/OpenJudge"
  sa-generated-at: "2026-04-14T00:00:00Z"
  sa-depends-on: []
---

# OpenJudge Grader 选型与组合

## 核心抽象

一个 grader 是一个异步函数：接收 kwargs，返回带 `score`（或 `rank`）和 `reason` 的结构化对象。

grader 不知道数据集，不知道其他 grader。它只处理单条样本（pointwise）或一组候选（listwise）。多 grader 编排、并发控制全部交给 `GradingRunner`。

## 选型决策树

```
评估维度能被确定性规则表达？
  → 是：FunctionGrader 或专用确定性 grader（StringMatch, Math, Code）

  → 否：需要 LLM 语义理解
      → 评估本身需要查外部信息/工具？
          → 是：AgenticGrader（成本是 LLMGrader 的 5-20 倍，仅工具查询才用）
          → 否：LLMGrader 或其预制子类
```

**输入数据决定 grader 族**：

| 你有什么 | 对应 grader 族 |
|---------|--------------|
| (query, response, ground_truth) | common/Correctness |
| (query, response) 无 ground truth | Hallucination, Relevance |
| 完整多轮对话历史 | multi_turn/ |
| Agent 工具调用轨迹（messages list） | agent/ |
| 图片 + 文本 | multimodal/ |
| 代码 + 测试用例 | code/CodeExecution |
| 数学表达式 | math/MathExpressionVerify |
| 两个候选比较优劣 | LISTWISE mode 任意 grader |

## 四大基类：怎么选

**FunctionGrader**：有确定性逻辑，不需要 LLM。同步/异步函数均可，装饰器或直接传函数。典型用途：exact match、规则 check、测试执行。

**LLMGrader**：需要语义理解。三个核心参数：`model`、`template`（变量在调用时填充）、`mode`。优先用 `response_format="json_object"` 或 structured output（Pydantic 模型）强制 JSON——这是减少解析失败的根本手段，而非依赖正则 fallback。template 变量缺失时抛 `KeyError`，不是 None。

使用 rubrics 参数时，LLMGrader 进入"RubricGrader 模式"——预定义可重复评分标准，同一 prompt 由不同调用者打分时一致性更高。没有 rubrics 时是"裸 LLM 判断"，适合无法事先描述评分标准的主观任务（如创意质量）。**当你需要可重复、可审计的评分时，应使用 rubrics 模式。**

```python
# RubricGrader 模式：可重复评分，用于礼貌度、专业性等有标准可描述的维度
grader = LLMGrader(
    model=model,
    template="Evaluate: {response}",
    rubrics=[
        {"score": 5, "description": "非常礼貌，使用敬语，积极友好"},
        {"score": 3, "description": "基本礼貌，无冒犯性语言"},
        {"score": 1, "description": "不礼貌或有冒犯性内容"},
    ]
)

# 裸 LLM 判断：适合难以预先定义标准的主观维度
grader = LLMGrader(model=model, template="Rate the creativity of: {response}")
```

**Pydantic 结构化输出（优先于 response_format）**：框架内置 `structured_model` 参数，比 `response_format="json_object"` 更可靠——由模型保证输出符合 schema，而非事后正则解析：

```python
from pydantic import BaseModel

class GradeOutput(BaseModel):
    score: int    # 1-5
    reason: str

grader = LLMGrader(
    model=model,
    template="Evaluate response quality: {response}\nOutput JSON with score (1-5) and reason.",
    structured_model=GradeOutput   # 优先用这个，不用 response_format
)
# structured_model 指定后，aevaluate 直接解析 Pydantic 对象，GraderError 率显著降低
```

`structured_model` 要求模型支持 function calling / tool use 接口（OpenAI 兼容格式）。不支持的模型仍需用 `response_format` + fallback 方案。

**LLM 解析失败三级 fallback 模板**：`response_format="json_object"` 不能完全避免解析失败（模型未严格遵守时仍输出自由文本）。生产中应在 FunctionGrader 或业务层做防御：

```python
import json, re

def parse_llm_output_with_fallback(raw_output: str, default_score: float = 0.0) -> float:
    """三级 fallback 解析 LLMGrader 输出"""
    # 第一级：直接 JSON 解析
    try:
        return float(json.loads(raw_output)["score"])
    except Exception:
        pass
    # 第二级：正则提取 JSON 块
    m = re.search(r'\{.*?"score"\s*:\s*([0-9.]+).*?\}', raw_output, re.DOTALL)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    # 第三级：关键词匹配（适用于 rubric 场景）
    for score_val, keywords in [(5, ["excellent", "perfect"]), (3, ["good", "ok"]), (1, ["bad", "poor"])]:
        if any(kw in raw_output.lower() for kw in keywords):
            return float(score_val)
    return default_score  # 兜底：返回默认值而非崩溃
```

**GraderError 统计与分布监控**：评估后必须检查各维度 GraderError 比例。error_rate > 10% 意味着 composite 权重已动态变化，分数不可信：

```python
# 评估后 GraderError 率统计（接在 arun 之后）
error_counts = {}
for grader_name, grader_results in results.items():
    if grader_name == "composite":
        continue
    errors = [r for r in grader_results if hasattr(r, "error")]
    error_counts[grader_name] = len(errors) / len(grader_results)
    if error_counts[grader_name] > 0.1:
        print(f"WARNING: {grader_name} error_rate={error_counts[grader_name]:.1%}，composite 权重已动态变化")

# 高错误率的常见原因
# - structured_model 不被模型支持 → 改用 response_format="json_object"
# - grader 不可 deepcopy（持有连接/句柄） → 每次调用重新初始化 grader
# - mapper 路径不存在 → 检查数据集字段名
# - API rate limit → 降低 max_concurrency（通常 10-20 最安全）
```

**AgenticGrader**：评估需要工具调用（搜索、代码执行、数据库）。先构建 agent 再传入 grader——这个顺序不能颠倒。输出解析有两级 fallback（JSON regex → 关键词 regex）；template 必须明确要求输出含 `score`/`rank` 字段的 JSON。

**直接继承 BaseGrader**：有特殊执行逻辑（代码执行、专用库、性能敏感匹配）。

## 预制 Grader 速查

**common/**（通用语义维度，1-5 量表）：
- `CorrectnessGrader`：与参考答案一致性（需要 reference_response）
- `HallucinationGrader`：凭空捏造检测（context 可选，无 ground truth）
- `RelevanceGrader`：切题性
- `HarmfulnessGrader`：有害内容
- `InstructionFollowingGrader`：指令遵循

Correctness vs Hallucination 的本质区别：Correctness 比较有 ground truth 的一致性；Hallucination 检测无中生有（可以没有 context）。

**code/**（分数范围 0-1 或 1-5）：
- `CodeExecutionGrader`：测试通过率（0-1）
- `CodeBugDetectionGrader`：LLM 分析 bug（1-5）
- 其余：SyntaxChecker, Security, Style, Complexity, PatchSimilarity

**确定性 grader**（0-1 或 0/1）：
- `StringMatchGrader`：算法在 `aevaluate` 调用时指定，不是初始化时
- `TextSimilarityGrader`：BLEU/ROUGE/METEOR/F1/cosine/jaccard
- `MathExpressionVerifyGrader`：支持 LaTeX 的数学等价验证（0/1）
- `ReasoningFormatGrader`：`<think>...</think><answer>...</answer>` 格式（0/1）

**multi_turn/**：需要完整 `history` 列表，不接受单条 response。ContextMemory, TopicSwitch, SelfCorrection, ResponseRepetition 等。

**agent/**：输入是 messages 列表（OpenAI 格式），工具调用在 `message["tool_calls"]` 字段。按能力维度分：tool/, trajectory/, action/, reflection/, memory/, observation/, plan/。

**skills/**：专为 AI Skill 包质量评估。SkillComprehensiveGrader（pointwise）、SkillComprehensivePairwiseGrader（pairwise）。

## Score 范围：最容易踩坑

不同 grader 的 score 范围完全不同：

| 类型 | 范围 |
|------|------|
| LLM grader（common/ + 大多数 agent/）| 1-5 |
| TrajectoryAccuracyGrader / TrajectoryComprehensiveGrader | **1-3**（注意：非 1-5）|
| StringMatch / MathVerify / CodeExecution | 0.0-1.0 |
| ReasoningFormat | 0.0 或 1.0 |

**TrajectoryGrader 量程是 1-3，不是 1-5**。与 ToolCallAccuracyGrader（1-5）混合聚合时须分别归一化：`trajectory_norm = (score - 1) / 2`，`tool_call_norm = (score - 1) / 4`。

框架没有全局归一化。直接平均不同 grader 的 score 是数学上无意义的操作。组合时必须显式归一化：

```python
# 1-5 量表归一化到 0-1（标准 min-max）
normalized = (score - 1) / 4

# 组合不同量纲 grader 的标准模式
correctness_raw = results["correctness"][i].score   # 1-5
similarity_raw  = results["similarity"][i].score    # 0-1

correctness_norm = (correctness_raw - 1) / 4
similarity_norm  = similarity_raw  # 已是 0-1

composite = 0.6 * correctness_norm + 0.4 * similarity_norm
```

## 评估策略（可选，默认 Direct）

| 策略 | 调用次数 | 用途 |
|------|---------|------|
| `DirectEvaluationStrategy` | 1 | 快速迭代 |
| `VotingEvaluationStrategy(num_votes=5)` | N | 离散分数稳定化（用奇数）|
| `AverageEvaluationStrategy(num_evaluations=3)` | N | 连续量表降噪 |

配置了 strategy 时，`aevaluate` 会 deepcopy 整个 grader（含 model）。本地大模型 + strategy 会有意外内存开销。

## 关键陷阱

**AgenticGrader 不等于"更好的 LLMGrader"**：它的价值是访问外部信息。只需语义理解时，AgenticGrader 更贵、更慢，且解析更脆弱。

**agent/ grader 与 AgenticGrader 是两件事**：
- `agent/` 目录的 grader（如 `ToolCallAccuracyGrader`）：评估被测 agent 的行为，自身是 LLMGrader 子类
- `AgenticGrader`：评估器本身是 agent，用工具来完成评估

**LISTWISE 模式不自动传入多候选**：template 需要显式包含 `{answer_1}`、`{answer_2}`，`GraderRank` 长度必须等于候选数量。

**multi_turn grader 不接受单条 response**：把 ContextMemoryGrader 用于单轮会得到无意义分数。

**threshold 参数不过滤**：`CorrectnessGrader(threshold=3)` 只把 threshold 放进 metadata，不会自动过滤低分。过滤需要在调用层判断 `result.score >= grader.threshold`。

**StringMatchGrader 算法在调用时指定**：
```python
grader = StringMatchGrader()  # 初始化时不固定算法
result = await grader.aevaluate(..., algorithm="substring_match")  # 这里指定
```
