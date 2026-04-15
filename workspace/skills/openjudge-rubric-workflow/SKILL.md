---
name: openjudge-rubric-workflow
description: >-
  OpenJudge rubric 生成策略与批量评估工作流指南。当需要为新评估任务生成评分标准、
  运行多 grader 批量评估、聚合多维度分数、分析评估结果时使用。覆盖三路径 rubric 生成、
  GradingRunner 工作流、Aggregator/Analyzer 体系和常见陷阱。
metadata:
  author: skill-anything
  version: "1.0"
  sa-source-repo: "https://github.com/agentscope-ai/OpenJudge"
  sa-generated-at: "2026-04-14T00:00:00Z"
  sa-depends-on: ["openjudge-grader-selection"]
---

# OpenJudge Rubric 生成与批量评估工作流

## Rubric 生成：三条路径的本质差异

选择哪条路径不是技术问题，而是**你拥有什么数据**：

| 路径 | 你有什么 | 适用场景 |
|------|---------|---------|
| **FunctionGrader** | 明确规则/算法 | 数学、代码执行、格式验证等确定性判断 |
| **SimpleRubricsGenerator** | 任务描述 + 少量样例 | 新任务冷启动、快速原型、无标注数据 |
| **IterativeRubricsGenerator** | 已标注偏好数据（query+response+score/rank） | 有历史标注、需要高质量 rubric |

IterativeRubricsGenerator 不是"更好的" SimpleRubricsGenerator。Simple 是零样本任务理解，Iterative 是从数据中归纳评估标准——解决的是完全不同的问题。

## SimpleRubricsGenerator：零样本生成

流程：task_description + scenario + sample_queries → 单次 LLM 调用 → 3-5 条 rubric → LLMGrader

配置关键点：
- `task_description`：越具体越好。"医疗问答系统" vs "评估放射科医生关于CT影像分析的回答质量" 效果差距巨大
- `sample_queries`：建议提供 3-5 条真实样例
- `min_score/max_score`：二元场景用 0-1，细粒度用 0-4；避免 0-10（粒度过细让 rubric 失去判别力）

生成失败时内置降级：回退到默认 rubric（准确性、相关性、完整性），不会崩溃。

**根本局限**：rubric 基于任务描述的语言理解，不基于实际数据分布。专业领域（医疗、法律）可能系统性偏错。

## IterativeRubricsGenerator：从数据中学习

### 第一阶段：Propose-Evaluate-Revise 循环

对每条训练样本：
1. Propose：生成 N 条 rubric
2. Evaluate：用生成的 rubric 对该样本重新评分
3. Validate：比较结果与 label，是否正确
4. Revise（失败时）：以失败原因为 feedback 要求改进
5. 循环直到验证通过或达到 max_epochs

验证失败的样本（rubric_valid=False）直接丢弃，不进入第二阶段。高难度/模糊样本会被自然过滤。

### 第二阶段：MCR² 聚合

采样模式自动切换：
- `≤100 样本`：ALL_SAMPLES，并发处理全部，收集所有有效 rubric
- `>100 样本`：SMART_SAMPLING，用 MCR²（最大编码率减少）迭代选择信息量最大的 rubric 子集

MCR² 依赖 **Dashscope TextEmbedding API**（阿里云）。没有 `DASHSCOPE_API_KEY` 时静默返回零向量，导致选择退化为随机——这是隐性依赖，国际环境需要注意。

SMART_SAMPLING 三个停止条件并行生效：
- 信息增益连续 `patience` 次（默认 2）< `min_increment_threshold`（默认 0.002）
- 选中 rubric 数达到 `max_total_rubrics`（默认 200）
- 迭代次数达到 `max_iterations`（默认 50）

实践建议：<500 样本中等规模，设 patience=1 + max_iterations=20 可显著降低成本，信息增益曲线通常前 5-10 次就趋于平稳。

### 触发条件与必须字段

IterativeRubricsGenerator 的最小可运行配置：
- 训练数据量：pointwise 至少 **20 条**有效样本（过少时 MCR² 向量空间退化）
- 字段名：pointwise 期望 `label_score`（不是 `score`），listwise 期望 `label_rank`
- 字段名错误时验证全部返回 False 但**不报错**，表现为 rubric 全部被丢弃

```python
# 正确的训练数据格式（pointwise）
train_data = [
    {"query": "...", "response": "...", "label_score": 4},  # label_score，非 score
    ...
]
generator = IterativeRubricsGenerator(model=model, min_score=1, max_score=5)
rubrics = await generator.agenerate(train_data)
```

**agenerate 返回空列表时的排查步骤**：

```python
# 1. 检查字段名是否正确
print(list(train_data[0].keys()))   # 必须含 label_score（pointwise）或 label_rank（listwise）

# 2. 如果字段名是 score 而非 label_score，重命名
train_data = [{"query": d["query"], "response": d["response"], "label_score": d["score"]} for d in raw_data]

# 3. 启用 verbose 查看每条样本验证日志
generator = IterativeRubricsGenerator(model=model, min_score=1, max_score=5, verbose=True)
rubrics = await generator.agenerate(train_data)   # 会打印每条 rubric_valid=True/False

# 4. 检查 DASHSCOPE_API_KEY（SMART_SAMPLING 模式必须，否则 MCR² 退化为随机选择）
import os
assert "DASHSCOPE_API_KEY" in os.environ, "MCR² 需要 Dashscope TextEmbedding API"
```

## 手写 vs 生成 Rubric 的决策

**手写优先**：监管要求/专业共识（医疗、法律）、团队已有清晰共识、数据量极少（<10 样本）。

**生成优先**：新任务快速建立基线、评估标准难形式化但数据中隐含、需要跨语言评估。

**最实用的混合策略**：
1. SimpleRubricsGenerator 生成草稿
2. 人工审查修改
3. 修改后 rubric 直接作为 LLMGrader 的 `rubrics` 参数
4. 有标注数据时用 IterativeRubricsGenerator 生成候选，与手写版对比验证

## 批量评估：GradingRunner

### 核心工作流

```python
# 1. 准备 dataset（每条包含 grader 所需字段）
dataset = [{"query": "...", "response": "...", "label_text": "..."}, ...]

# 2. 定义 grader_configs（名称 → 配置，必须是 dict 格式，不能是 list）
#    多 grader 并行：同一 dataset 上同时运行多个评估维度
configs = {
    "correctness": GraderConfig(
        grader=CorrectnessGrader(model=model),
        mapper={"reference_response": "label_text"},   # grader参数名 → 数据集字段名
    ),
    "relevance": GraderConfig(grader=RelevanceGrader(model=model)),
    "format_ok": GraderConfig(
        grader=FunctionGrader(func=lambda response, **_: 1.0 if response.strip() else 0.0),
    ),
}

# 3. 归一化处理（WeightedSumAggregator 不做归一化，是用户责任）
# correctness/relevance 为 1-5 量表，format_ok 为 0-1，量纲不一致时需归一化
# 方式：用 FunctionGrader 将 1-5 量表包装为 0-1，或在 aggregator weights 中补偿

# 4. 定义 aggregator 时先校验 key 覆盖（防止静默 weight=0 bug）
grader_names = set(configs.keys())
weight_keys  = {"correctness", "relevance", "format_ok"}
assert weight_keys == grader_names, (
    f"权重 key 与 grader 名称不一致：多余={weight_keys-grader_names}，缺失={grader_names-weight_keys}"
)
aggregator = WeightedSumAggregator(
    "composite",
    weights={"correctness": 0.6, "relevance": 0.3, "format_ok": 0.1}
)

# 5. 运行
runner = GradingRunner(grader_configs=configs, max_concurrency=16, aggregators=[aggregator])
results = await runner.arun(dataset)
# results["correctness"]、results["relevance"]、results["format_ok"]、results["composite"]
# 各是等长 List[GraderResult]

# 6. 检查 GraderError 比例（error 会导致动态重加权，composite 静默失真）
for grader_name in configs:
    errors = sum(1 for r in results[grader_name] if hasattr(r, "error"))
    if errors > 0:
        print(f"WARNING: {grader_name} 有 {errors}/{len(dataset)} 条 GraderError，composite 权重已动态变化")
```

### 并发模型理解

`max_concurrency=32` 控制的是同时等待 LLM API 响应的协程数，不是 CPU 线程数。所有协程已创建，semaphore 控制它们进入 API 调用的时机。

同一 GradingRunner 实例跨 `arun_multiple_datasets()` 调用时 semaphore 是共享的——多数据集共用同一并发池。

每次 `_arun()` 调用都 deepcopy grader，确保并发不互相污染状态。grader 内部若持有不可 deepcopy 的对象（数据库连接、文件句柄），会静默返回 `GraderError`。

### Mapper 两种语义

**Dict mapper**（`{"grader_param": "data.path.field"}`）：点号路径提取 + 重命名。对列表类型自动遍历，路径不存在时返回 None（不报错，grader 收到 None 可能静默出错）。

**Callable mapper**（函数）：完全控制，可做任意变换（字段拼接、条件逻辑），必须返回 key 与 grader 参数名精确匹配的 dict。

### Aggregator：样本级操作

**WeightedSumAggregator 不做归一化**——量纲归一化是用户责任。直接聚合不同量纲的 grader（如 1-5 量表与 0-1 量表）会产生数学上无意义的 composite 分数，且不报错。归一化应在 grader 层完成：

```python
# 用 FunctionGrader 包装 LLMGrader 并归一化输出（完整可运行示例）
from openjudge import FunctionGrader, CorrectnessGrader

llm_grader = CorrectnessGrader(model=model)

async def normalized_correctness(query: str, response: str, reference_response: str, **_) -> float:
    """调用 LLMGrader 并将 1-5 归一化到 0-1"""
    result = await llm_grader.aevaluate(
        query=query, response=response, reference_response=reference_response
    )
    if hasattr(result, "error"):
        return 0.0   # GraderError 时返回默认值
    return (result.score - 1) / 4   # 1-5 → 0-1

# 这个 grader 输出 0-1，可与其他 0-1 grader 直接聚合
norm_correctness_grader = FunctionGrader(func=normalized_correctness)
# 配合 format_check（也是 0-1）直接 WeightedSum，无量纲问题
configs = {
    "correctness_norm": GraderConfig(grader=norm_correctness_grader),
    "format_ok": GraderConfig(grader=FunctionGrader(func=lambda response, **_: 1.0 if response.strip() else 0.0)),
}
aggregator = WeightedSumAggregator("composite", weights={"correctness_norm": 0.7, "format_ok": 0.3})
```

WeightedSumAggregator 其他关键细节：
- 权重只有比例意义，不需要归一化到 1（代码内部会 `weighted_sum / total_weight`）
- `GraderRank` 结果**不参与数值计算**。全 listwise 模式时 composite 输出 0.0（静默语义错误）
- **`GraderError` 的权重被忽略，等于动态重加权**——单条记录验证失败（ValidationError → GraderError）时，该 grader 权重从分母中移除，导致 composite 分数的维度比例静默变化。部分 grader 批量失败时，composite 分数已不等价于预期的加权组合，但数值仍输出（不崩溃、不报警）。防御方式：评估后检查各维度 GraderError 数量与比例。

aggregator 名称不能与任何 grader key 相同，否则覆盖 grader 结果。

### 二阶段评估策略：先廉价过滤再精确评估

预算有限时，不要对所有样本都调 LLMGrader。先用确定性 grader（ExactMatch/StringMatch）快速过滤：

- ExactMatch/StringMatch 通常能**过滤 30-60% 的样本**（直接正确或明显错误）
- 只对"模糊区域"的样本才调 LLMGrader，显著降低 API 成本

**任务类型决定过滤效果**：

| 任务类型 | 推荐一阶段 grader | 预期过滤率 | 说明 |
|---------|----------------|----------|------|
| 知识问答/事实类 | StringMatch（substring/fuzzy） | 40-60% | 答案有明确关键词 |
| 选择题/分类 | ExactMatch | 50-70% | 答案空间小 |
| 代码生成 | SyntaxChecker + CodeExecutionGrader | 20-40% | 语法通过后仍需语义判断 |
| 开放式问答/创意类 | 无（直接 LLMGrader） | < 5% | StringMatch 几乎无法过滤 |

**阈值设置原则**：StringMatchGrader 触发 LLM 的阈值不应设在 1.0（完全匹配）。fuzzy 匹配时，`score < 0.6` 触发 LLM 是经验安全值；substring 匹配时用 `score == 0.0`（完全未命中）触发。

```python
# 二阶段示例（含阈值配置）
exact_grader = StringMatchGrader()
llm_grader = CorrectnessGrader(model=model)

FUZZY_THRESHOLD = 0.6   # fuzzy 分数 < 0.6 时进入 LLM 判断

for sample in dataset:
    exact_result = await exact_grader.aevaluate(**sample, algorithm="fuzzy_match")
    if exact_result.score >= FUZZY_THRESHOLD:
        final_score = exact_result.score   # 高置信匹配，无需 LLM
    else:
        llm_result = await llm_grader.aevaluate(**sample)
        final_score = (llm_result.score - 1) / 4
```

## Analyzer 体系

诊断路径：先 Distribution → 再 Consistency → 再 Validation（Correlation + Accuracy）。Consistency 不合格时 Validation 分析无意义。

**Statistical Analyzers（不需要 ground truth）**：
- `DistributionAnalyzer`：检查分数分布，stdev 极低说明 grader 缺乏区分度
- `ConsistencyAnalyzer`：同一批样本跑两次，Pearson 相关 < 0.7 说明 grader 不稳定

**Validation Analyzers（需要 ground truth）**：AccuracyAnalyzer, CorrelationAnalyzer 等。用于标定 grader 与人类判断的一致性。

**新 grader 的完整验证流程模板**：

```python
from openjudge.analyze import ConsistencyAnalyzer, CorrelationAnalyzer, AccuracyAnalyzer

# 前提：dataset 中含 label_score（人类标注），grader 已完成两轮独立评估
# run1_results, run2_results = List[GraderResult]，从同一 dataset 两次 arun 获得

# 第一步：一致性（Consistency）— 通过阈值：Pearson ≥ 0.75
consistency = ConsistencyAnalyzer()
# 显式传三个参数（2参数旧接口兼容逻辑不健壮）
c_result = consistency.analyze(dataset, run1_results, run2_results)
assert c_result.pearson >= 0.75, f"Grader 不稳定，Pearson={c_result.pearson:.3f}，停止后续验证"

# 第二步：相关性（Correlation）— 通过阈值：Pearson ≥ 0.75
correlation = CorrelationAnalyzer(label_key="label_score")
cor_result = correlation.analyze(dataset, run1_results)
print(f"与人类标注相关性 Pearson={cor_result.pearson:.3f}")

# 第三步：精确率（Accuracy）— 通过阈值：accuracy ≥ 0.70
accuracy = AccuracyAnalyzer(label_key="label_score")
acc_result = accuracy.analyze(dataset, run1_results)
print(f"精确率 accuracy={acc_result.accuracy:.3f}")

# 通过标准
PASS = c_result.pearson >= 0.75 and cor_result.pearson >= 0.75 and acc_result.accuracy >= 0.70
print("Grader 验证通过" if PASS else "Grader 验证未通过，需调整 rubric 或模型")
```

**ConsistencyAnalyzer 接口陷阱**：有 2 参数旧接口和 3 参数新接口，兼容逻辑基于空值判断不够健壮。使用时显式传三个参数：`analyze(dataset, grader_results, another_grader_results)`。

**多场景验证指标选择**：根据 grader 输出类型选用不同统计指标：

| Grader 输出类型 | 推荐指标 | 通过阈值 |
|--------------|---------|---------|
| 连续分数（LLMGrader 1-5）| ConsistencyAnalyzer + CorrelationAnalyzer + AccuracyAnalyzer | Pearson ≥ 0.75（一致性），Pearson ≥ 0.75（相关性），accuracy ≥ 0.70 |
| 二分类（pass/fail，0/1）| Cohen's Kappa + 准确率 | Kappa ≥ 0.6（中等一致），accuracy ≥ 85% |
| 排名（listwise，1-N）| Kendall's Tau | Tau ≥ 0.7 |

```python
# 二分类 grader 验证示例（使用 sklearn）
from sklearn.metrics import cohen_kappa_score, accuracy_score
valid_results = [(d["label_score"], r.score) for d, r in zip(dataset, run1_results) if not hasattr(r, "error")]
labels = [v[0] for v in valid_results]
preds  = [v[1] for v in valid_results]
kappa  = cohen_kappa_score(labels, preds)       # 目标 ≥ 0.6
acc    = accuracy_score(labels, preds)           # 目标 ≥ 0.85

# 排名 grader 验证示例（使用 scipy）
from scipy.stats import kendalltau
tau, p_value = kendalltau(labels, preds)        # 目标 ≥ 0.7
```

**量纲归一化前置要求**：连续分数 LLMGrader 使用 AccuracyAnalyzer 时，若 grader 输出 1-5 而人工标注也是 1-5，可直接比较；若两者量纲不同，须先归一化再传入 AccuracyAnalyzer，否则精确匹配率恒为 0。

## 常见陷阱速查

| 陷阱 | 表现 | 根因 |
|------|------|------|
| 所有样本 GraderError | 结果全是 error | grader 不可 deepcopy，或 mapper 路径不存在 |
| composite 全是 0.0 | aggregator 输出零 | grader 返回 GraderRank，WeightedSum 不处理 |
| accuracy 计算基数异常小 | label 静默跳过 | label_path 在部分样本中不存在 |
| 大量限流报错 | rate limit GraderError | max_concurrency 超过 API QPS 限制（通常设 10-20 安全）|
| 多数据集并发无效 | arun_multiple_datasets 没提速 | 单数据集规模小于 max_concurrency，填不满 semaphore |
