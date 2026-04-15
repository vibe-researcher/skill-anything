# Runner、Aggregator 与批量评估工作流：领域认知模型

## 核心设计哲学

OpenJudge 的批量评估体系围绕一个根本问题而设计：**如何在 LLM 评估的天然不确定性和高延迟中，安全、可并发地把多个 grader 应用于大规模数据集，并从结果中提取有意义的信号？**

架构上采用"扁平并发、结构化输出"策略：不是逐样本串行执行，也不是按 grader 批次执行，而是把 `N graders × M samples` 的笛卡尔积展平为一个协程列表，一次性提交给事件循环，由 Semaphore 统一节流。这个决策的含义很深：结果顺序由 `coroutine_info` 重建，而非依赖执行顺序——意味着执行过程中任何乱序都不会污染结果。

---

## GradingRunner 的真正含义

### 并发模型的隐性约束

`max_concurrency=32` 不是"最多同时运行 32 个样本"，而是"最多同时持有 32 个正在等待 LLM 响应的协程"。这个数字控制的是 **外部 API 调用的并发度**，不是 CPU 线程数。

关键点：
- `SemaphoreResourceExecutor` 的 semaphore 在 `grader.aevaluate()` 的内部调用 `executor.submit()` 时才真正生效
- 所有协程在 `asyncio.gather()` 时已经全部创建，semaphore 控制的是它们进入 LLM API 调用的时机
- **同一个 GradingRunner 实例跨 `arun_multiple_datasets()` 调用时，semaphore 是共享的**——多个数据集的评估共用同一个并发池，这是设计预期而非 bug

### 状态隔离的隐性保证

每次 `_arun()` 调用都做 `copy.deepcopy(grader)`，这是一个重要的保证：grader 内部可能持有状态（如对话历史、缓存），deepcopy 确保并发评估不会互相污染。代价是内存开销，但对于正确性是必要的。

**陷阱**：如果 grader 内部持有不可 deepcopy 的对象（如数据库连接、文件句柄），`_arun()` 会在 deepcopy 阶段静默失败，返回 `GraderError`。设计复杂 grader 时必须考虑 deepcopy 兼容性。

### Runner vs 直接调 aevaluate 的判断标准

直接调 `grader.aevaluate()` 适合：
- 单样本调试
- 需要细粒度控制每次调用的场景
- 已有自己的并发管理逻辑

使用 `GradingRunner` 的场景：
- 需要同时运行多个 grader（评估维度 > 1）
- 数据集规模 > 10 条，需要进度条和统一错误处理
- 需要 aggregator 把多维度分数合并为综合得分
- 需要跨数据集的统一并发控制

---

## 数据流契约：最容易出错的地方

### Dataset 的隐性要求

`dataset` 只要求是 `List[dict]`，但实际契约比这复杂：
1. 每个 dict 的字段需要与 grader 的 `aevaluate()` 参数名对齐，或者通过 mapper 转换
2. 对于 Validation Analyzers，dict 中必须包含 `label` 字段（默认路径），否则该样本被静默跳过（不报错）
3. 对于 PairwiseAnalyzer，dict 必须包含 `metadata.model_a` 和 `metadata.model_b`——这是 analyzer 的输入契约，不是 grader 的

**常见陷阱**：evaluation_data 和 metadata 的分层结构。`pairwise_evaluation.py` 的范例展示了推荐模式：
```python
{
    "evaluation_data": { "instruction": ..., "response_a": ..., "response_b": ... },
    "metadata": { "model_a": "gpt-4", "model_b": "claude", "order": "original" }
}
```
mapper 从 `evaluation_data.*` 提取 grader 输入，analyzer 从 `metadata.*` 提取分析所需的上下文。这个分层让 grader 无需感知元数据，analyzer 无需感知评估数据。

### Mapper 的两种语义

**Dict mapper**（`{"grader_param": "data.path.field"}`）：
- 使用点号路径从嵌套 dict 中提取字段并重命名
- `get_value_by_path` 有一个特殊行为：对列表类型自动遍历，`"items.name"` 会从列表中每个元素提取 `name` 字段，返回列表。这在 listwise grader 中非常有用，但如果数据结构意外地是列表，可能产生意外行为。
- 路径不存在时返回 None，不抛异常——grader 收到 `None` 可能静默产生错误结果

**Callable mapper**（函数）：
- 完全控制变换逻辑，适合需要计算派生字段的场景
- 函数必须返回 dict，且 dict 的 key 必须与 grader 的参数名精确匹配

**关键区别**：dict mapper 只做字段提取和重命名，callable mapper 可以做任意变换（包括拼接字段、条件逻辑等）。

### GraderConfig 的多种创建方式

```python
# 等价的四种写法
GraderConfig(grader=my_grader, mapper={"q": "query"})
GraderConfig.create(my_grader)                          # 无 mapper
GraderConfig.create((my_grader, {"q": "query"}))        # tuple
GraderConfig.create({"grader": my_grader, "mapper": {"q": "query"}})  # dict
```

`grader_configs` 参数接受 `Dict[str, ...]`，key 是 grader 名称，会成为 RunnerResult 的 key。命名要谨慎——aggregator 名称不能与 grader 名称冲突，否则会覆盖。

---

## Aggregator 体系：多维度得分的设计决策

### 聚合的正确位置

Aggregator 在 GradingRunner 内部，在所有样本的 grader 结果都完成后，**逐样本**调用，生成额外的结果列。最终 `RunnerResult` 包含：
- 每个 grader 的结果列
- 每个 aggregator 的结果列（每个元素是该样本的聚合分数）

这个设计意味着 aggregator 是**样本级别**的操作，不是数据集级别的。想要数据集级别的聚合（如平均分），应该在 `arun()` 返回后手动处理，或用 Analyzer。

### WeightedSumAggregator 的语义细节

- `weights` 参数中权重**不会自动归一化**——权重之和决定结果的数值范围。如果 weights 是 `{"a": 0.3, "b": 0.7}`，总权重=1.0，最终分数在 [0,1]；如果是 `{"a": 3, "b": 7}`，最终分数也在 [0,1] 因为代码做了 `weighted_sum / total_weight`。**所以权重只有比例意义，不需要归一化到 1。**
- `GraderRank` 类型的结果被记录在 metadata 中但**不参与数值计算**，`total_weight` 不会增加。如果所有 grader 都返回 Rank（listwise 模式），最终 `final_score` 为 0.0——这是静默的语义错误。
- `GraderError` 的权重被忽略（`total_weight` 不增加），等于动态重加权。这意味着部分 grader 失败不会导致聚合结果失效，但会改变权重比例——这在某些场景下可能是错误行为。

### 自定义 Aggregator

`BaseAggregator.__call__` 接收单个样本的所有 grader 结果 `Dict[str, GraderResult]`，返回单个 `GraderResult`。可以实现 MinAggregator（取最低分，保守评估）、MaxAggregator、VotingAggregator 等。

---

## Analyzer 体系：两类分析的认知模型

### Statistical vs Validation：根本区别

**Statistical Analyzers**（`DistributionAnalyzer`, `ConsistencyAnalyzer`）：
- **不需要 ground truth**
- 只看 grader 输出本身的分布特征
- 用于诊断 grader 的行为，而非验证模型质量

**Validation Analyzers**（`AccuracyAnalyzer`, `PrecisionAnalyzer`, `RecallAnalyzer`, `F1Analyzer`, `FalsePositiveAnalyzer`, `FalseNegativeAnalyzer`, `CorrelationAnalyzer`）：
- **必须有 ground truth**（通过 `label_path` 参数指定）
- 用于评估 grader 相对于已知答案的准确性
- 常见于 grader 自我验证场景（验证 LLM-as-judge 的可靠性）

### 识别模型弱点的实践路径

1. **先 DistributionAnalyzer**：检查得分是否集中在某个区间（如全部 0.8-0.9），stdev 极低说明 grader 缺乏区分度，不是模型本身的问题。

2. **再 ConsistencyAnalyzer**：对同一批样本跑两次，计算 Pearson 相关。consistency < 0.7 说明 grader 不稳定，结果不可信，此时其他分析都没有意义。

3. **分维度对比**：如果多个 grader 给同一模型打分，对比不同维度的得分分布，找到得分异常低的维度——这才是模型的真实弱点，而非所有维度的平均。

4. **Validation Analyzers 用于标定 grader**：在有少量人工标注数据时，用 AccuracyAnalyzer 检验 grader 与人类判断的一致性，确保自动评估的可信度。

### ConsistencyAnalyzer 的接口陷阱

该类有一个遗留的破坏性改动：原来是 2 参数调用 `analyze(first_run, second_run)`，现在是 3 参数 `analyze(dataset, grader_results, another_grader_results)`。代码中有兼容逻辑，但判断条件依赖参数是否为空，可能产生误判。使用时**必须明确传入三个参数**以使用当前 API。

---

## 端到端工作流的正确模式

### 标准批量评估模式

```python
# 1. 准备 dataset（每条包含 grader 所需字段）
dataset = [{"query": "...", "answer": "...", "label": 1}, ...]

# 2. 定义 grader_configs（名称 → 配置）
configs = {
    "accuracy": GraderConfig(grader=AccuracyGrader(), mapper={"q": "query", "a": "answer"}),
    "relevance": GraderConfig(grader=RelevanceGrader()),  # 字段名恰好匹配，无需 mapper
}

# 3. 定义 aggregator（可选）
aggregator = WeightedSumAggregator("composite", weights={"accuracy": 0.6, "relevance": 0.4})

# 4. 运行
runner = GradingRunner(grader_configs=configs, max_concurrency=16, aggregators=[aggregator])
results = await runner.arun(dataset)

# 5. 结果结构：results["accuracy"], results["relevance"], results["composite"]
# 每个 value 是与 dataset 等长的 List[GraderResult]

# 6. 分析（针对每个 grader 分别分析）
dist = DistributionAnalyzer().analyze(dataset, results["composite"])
# 如果有 label：
acc = AccuracyAnalyzer().analyze(dataset, results["accuracy"], label_path="label")
```

### 多数据集对比模式

```python
# 评估同一个模型在不同任务上的表现
task_datasets = [task1_data, task2_data, task3_data]
all_results = await runner.arun_multiple_datasets(task_datasets)
# all_results[i] 对应 task_datasets[i] 的 RunnerResult
```

**注意**：`arun_multiple_datasets` 内部对每个数据集调用 `arun()`，所有数据集共用同一个 semaphore，总并发度受 `max_concurrency` 控制。这适合"多任务并发评估"场景，不适合"相同数据集反复评估取平均"场景（后者应在外部循环）。

### Pairwise 评估的特殊约定

Pairwise 评估在 OpenJudge 中用 POINTWISE 模式的 LLMGrader 实现（score=1.0 表示 A 赢，0.0 表示 B 赢）。关键约定：
- 每个 pair 做两次对比（原始顺序 + 交换顺序），消除 position bias
- dataset 中的 `metadata` 字段携带 model_a/model_b 信息，供 PairwiseAnalyzer 解读
- Analyzer 而非 Grader 负责把原始分数转换为 win rate——grader 只知道"谁赢"，不知道"为什么"

---

## 常见陷阱总结

| 陷阱 | 表现 | 根因 | 解决 |
|------|------|------|------|
| 所有样本返回 GraderError | 结果全是 error | grader 不可 deepcopy，或 mapper 路径不存在 | 检查 grader.__init__ 中是否有不可序列化对象；验证 mapper 路径 |
| Aggregator 输出全是 0.0 | composite 分全零 | grader 返回 GraderRank 而非 GraderScore | WeightedSumAggregator 只处理 GraderScore |
| 并发设置太高被限流 | 大量 GraderError + rate limit 报错 | max_concurrency 超过 API 限速 | 根据 API QPS 限制设置，通常 10-20 是安全范围 |
| 标签静默跳过 | accuracy 计算基数异常小 | label_path 不存在于部分样本 | 检查 `total_predictions` 是否等于 dataset 长度 |
| arun_multiple_datasets 速度未提升 | 多数据集并发无效 | 单个数据集本身规模过小，无法填满 semaphore | 适用于每个数据集 > max_concurrency 条的场景 |
| GraderConfig 名称冲突 | aggregator 结果覆盖 grader 结果 | aggregator.name 与某个 grader key 相同 | aggregator 名称应与所有 grader key 不同 |
| ConsistencyAnalyzer 误用老接口 | 结果可能基于错误的参数配对 | 2/3 参数接口兼容逻辑基于空值判断，不够健壮 | 显式传三个参数：dataset, grader_results, another_grader_results |
