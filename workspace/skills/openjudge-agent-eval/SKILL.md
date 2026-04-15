---
name: openjudge-agent-eval
description: >-
  OpenJudge Agent 全生命周期评估与高级集成指南。当需要评估 Agent 的工具调用轨迹、设计 Pairwise/Auto Arena
  评估方案、接入 LangSmith/Langfuse 监控、或在 VERL RL 训练中使用 OpenJudge reward 时使用。
metadata:
  author: skill-anything
  version: "1.0"
  sa-source-repo: "https://github.com/agentscope-ai/OpenJudge"
  sa-generated-at: "2026-04-14T00:00:00Z"
  sa-depends-on: ["openjudge-grader-selection", "openjudge-rubric-workflow"]
---

# OpenJudge Agent 评估与高级集成

## Agent 评估的根本原则

传统 grader 评估一个输入→输出对。Agent 的本质是多轮决策序列：每一步选择工具、形成观测、更新状态。只看最终答案等于只看棋局结果而不看棋局。

**评估 Agent 必须把 messages 历史作为一等公民输入**，而不是把最终答案剥离出来。

agent/ 目录下所有 grader 的 `aevaluate` 接受 `messages: List[Dict]`（OpenAI 格式），工具调用信息在 `message["tool_calls"]` 字段中。

## Agent 评估 Grader 选型矩阵

三类 grader 面向不同的评估维度，通常组合使用：

| 评估需求 | 推荐 Grader | 评分量程 | 典型场景 |
|---------|-------------|---------|---------|
| 单步工具调用是否正确 | `ToolCallAccuracyGrader` / `ToolCallPrecisionRecallGrader` | **1-5** | API 参数验证、工具选择准确性、参数格式审计 |
| 全链路决策质量（推荐主评估） | `TrajectoryComprehensiveGrader` / `TrajectoryAccuracyGrader` | **1-3** | 外部 API 调用、多步规划、路径效率+逻辑一贯性+目标对齐 |
| 最终结果/输出验证 | `CorrectnessGrader`（LLMGrader 子类） | **1-5** | 最终答案与参考答案对比，无法区分过程 |

**注意量纲差异**：TrajectoryGrader（1-3）与 ToolCallAccuracyGrader（1-5）量纲不同，组合使用时须分别归一化到 0-1 再加权：

```python
# 量纲归一化后再加权
trajectory_norm = (trajectory_result.score - 1) / 2    # 1-3 → 0-1
tool_call_norm  = (tool_call_result.score - 1) / 4      # 1-5 → 0-1
composite = 0.6 * trajectory_norm + 0.4 * tool_call_norm
```

评估外部 API 调用场景的推荐组合：`TrajectoryComprehensiveGrader`（主，权重 0.6）+ `ToolCallAccuracyGrader`（辅，权重 0.4）。仅看最终输出是不够的——工具路径正确但答案随机正确的 agent 与真正理解任务的 agent 无法区分。若只选一个，优先选 `TrajectoryComprehensiveGrader`，因为外部 API 调用轨迹的核心是整体决策路径质量，而非单步工具精度。

## Agent 评估维度与对应 Grader

**Trajectory（轨迹整体质量）**：
- `TrajectoryAccuracyGrader`：关键步骤是否都走到了
- `TrajectoryComprehensiveGrader`：路径效率 + 逻辑一贯性 + 目标对齐（综合）

**Tool Use（工具调用行为）**：
- `ToolCallAccuracyGrader`：工具选择正确性
- `ToolCallPrecisionRecallGrader`：调用精确率/召回率
- `ToolCallStepSequenceGrader`：调用顺序是否合理
- `ActionLoopDetectionGrader`：是否陷入重复调用循环
- `ObservationInformationGainGrader`：每次工具调用是否带回新信息

**Memory（记忆能力）**：MemoryAccuracyGrader, DetailPreservationGrader, RetrievalEffectivenessGrader

**Reflection（反思能力）**：准确性、结果理解、进度感知

**Plan（规划质量）**：计划可行性（往往需要人工审查或专用 LLM-as-Judge）

## AgenticGrader：让 Grader 本身成为 Agent

当评估工具性事实（"这句话是否符合最新数据？"）时，静态 LLM judge 受限于知识截止日期。AgenticGrader 让 grader 先用工具查，再做判断。

**必须先构建 agent，再传入 grader**：

```python
agent = ReActAgent(model=model, tools=[WebSearchTool()], max_iterations=10)
grader = AgenticGrader(agent=agent, template="Evaluate: {response}\n\nOutput JSON: {\"score\": N, \"reason\": \"...\"}")
```

不要在 grader 里配置 model/tools，AgenticGrader 不接受这些参数。

AgenticGrader 的 template 同时提供 system prompt 和 user 消息，不需要也不应该在 ReActAgent 初始化时再设置 system prompt。

template 必须明确要求输出含 `score` 或 `rank` 字段的 JSON。内置两级 fallback 解析（JSON regex → 关键词 regex），两者都失败才抛 ValueError。

**max_iterations 推荐值矩阵**：过低会导致复杂任务评估不完整（JSON 未输出就终止），过高浪费成本。

| 任务复杂度 | max_iterations | 典型场景 |
|-----------|---------------|---------|
| 简单查询（1-2 步工具） | 5-8 | 单次搜索验证、格式检查 |
| 中等复杂（3-5 步） | 10-15 | 多步事实核查、代码分析 |
| 高复杂度（多轮推理） | 20-30 | 深度研究、多来源交叉验证 |

降低 max_iterations 节省成本，但若 agent 在迭代耗尽前未生成 JSON 输出，会触发 ValueError。建议先用较高值跑少量样本，观察实际平均迭代次数后再收紧。

**AgenticGrader 四种互补成本控制机制**：

1. **max_iterations 分级硬上限**（核心控制点）：见上表，生产默认推荐 10。
2. **max_tool_calls 独立计数**（工具级精细控制）：已有 `tool_calls_count` 字段，增加早停：`if agent.tool_calls_count >= max_tool_calls: break`，防止单次评估工具调用数失控。
3. **asyncio.Semaphore 批量并发限制**（系统层面）：控制同时运行的 AgenticGrader 数量，防止批量评估并发费用失控：
   ```python
   sem = asyncio.Semaphore(10)  # 最多 10 个 AgenticGrader 并发
   async def bounded_eval(sample):
       async with sem:
           return await grader.aevaluate(**sample)
   results = await asyncio.gather(*[bounded_eval(s) for s in dataset])
   ```
4. **truncate_tool_output 减少 context 长度**：默认 4000 字符，设 1000-2000 可减少约 50% 工具输出 token，对长输出工具（搜索结果、代码执行）效果显著。

三者组合（max_iterations 收紧 + Semaphore 并发控制 + truncate_tool_output）可降低 50-80% 总 API 费用。

**工具输出序列化陷阱**：工具的 `aexecute` 必须返回结构化数据，不能用 `str(result)` 序列化后返回。

```python
# 错误：str() 序列化导致调用方 result['score'] 报 TypeError
async def aexecute(self, **kwargs) -> str:
    result = await compute(...)
    return str(result)   # 反模式：把 dict 变成字符串

# 正确：直接返回可序列化的 dict
async def aexecute(self, **kwargs) -> dict:
    return {"score": 0.8, "details": "..."}
```

## 工具接入：四种方式

| 方式 | 适用场景 |
|------|---------|
| 原生 `BaseTool` 子类（实现 `schema` + `aexecute`） | 生产稳定性优先，零依赖 |
| `FunctionToolAdapter` | 快速包装现有 Python 函数 |
| `LangChainToolAdapter` | 用 LangChain 工具库，保留 OpenJudge 推理 |
| `LangChainAgentAdapter` / `AgentScopeAgentAdapter` | 整体委托给已有第三方 agent |

**适配器在 cookbook，不在核心库**：`LangChainAgentAdapter` 在 `cookbooks/agentic_grader/adapters/`，需手动复制到项目使用，不能从 `openjudge.agentic` 导入。

## Pairwise / Auto Arena 评估

### 适用场景

- 没有标注数据的新领域（用胜率代替绝对分）
- 主观任务（创意写作、对话质量）
- 模型版本迭代对比
- 有明确正确答案的任务（改用 CorrectnessGrader）、模型数超过 10 个（比较次数爆炸 N²/2）时不适用

### 五步 Pipeline

```
QueryGenerator → ResponseCollector → TaskBasedRubricGenerator → 全配对双向比较 → PairwiseAnalyzer
```

**双向比较消除位置偏差**：每对 (A, B) 做两次：原顺序 + 交换顺序。只有两次一致才算有效胜负。跳过双向比较是系统性错误，LLM 倾向于偏好第一个响应。

**胜率矩阵优于单一排名**：可以看到"A 在 X 类任务强，B 在 Y 类任务强"，而非线性排名。

**实践规模控制**：5 个模型 → 20 次比较（双向）。建议控制在 5-8 个模型以内。

### Pairwise 数据格式约定

```python
{
    "evaluation_data": {"instruction": ..., "response_a": ..., "response_b": ...},
    "metadata": {"model_a": "gpt-4", "model_b": "claude", "order": "original"}
}
```

mapper 从 `evaluation_data.*` 提取 grader 输入，PairwiseAnalyzer 从 `metadata.*` 提取分析上下文。grader 不感知元数据，analyzer 不感知评估数据。

## 评估策略选择

| 策略 | 调用次数 | 适用场景 |
|------|---------|---------|
| `DirectEvaluationStrategy` | 1 | 快速迭代、探索阶段 |
| `AverageEvaluationStrategy(N)` | N（并发）| 连续分数降噪 |
| `VotingEvaluationStrategy(N)` | N（并发）| 离散分数稳定化（用奇数 N，推荐 5）|
| `GRPOTournamentEvaluationStrategy` | N*(N-1)/2 | RL 训练 reward（输出 [-1,1] 相对胜率）|

所有 N 次调用是并发执行的，不是串行。GRPO 输出的是组内相对排序，同一响应在不同对手群体中 reward 不同——这是设计意图。

何时升级策略：评估结果方差大 → 切换到 Voting/Average；用于 RL 训练 → 必须用 GRPOTournament。

## 外部集成

### LangSmith：开发阶段 Benchmark

LangSmith evaluator 是同步函数，OpenJudge grader 是异步的。需要 `asyncio.run()` 桥接：

```python
def evaluator(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
    result = asyncio.run(grader.aevaluate(**mapper(inputs, outputs)))
    return {"score": result.score, "comment": result.reason}
```

在已有 event loop 的环境（Jupyter、FastAPI）中 `asyncio.run()` 会报错，用 `nest_asyncio.apply()` 解决。

Mapper 映射 LangSmith 格式（`inputs.*`, `outputs.*`）到 grader 参数名。

### Langfuse：生产监控

Langfuse 以 trace 为中心（从线上运行记录取数据），LangSmith 以 dataset 为中心。

```
线上运行（@observe 装饰）→ Langfuse 存 trace → langfuse.api.trace.list() → OpenJudge grader → langfuse.create_score(trace_id=...)
```

天然支持持续监控、历史数据离线评估、增量评估（time range filter）。生产监控优先选 Langfuse，开发阶段 benchmark 优先选 LangSmith。

### VERL RL 训练集成

适合条件：任务复杂（单一 reward 信号不足）+ 已有 VERL 基础设施 + 愿意承担 LLM judge API 成本。

三层架构：RewardManager（VERL 适配，token decoding + tensor filling）→ RewardFunction（业务逻辑）→ GradingRunner + Graders（评估层）。

**Fresh Instance Pattern**：每次调用 `compute_batch_scores` 必须创建新的 `GradingRunner` 实例，不能复用。async event loop 在不同调用间可能变化。

推荐配比：rule-based grader 负责惩罚明显错误（loop、信息无增益），LLM grader 负责评估整体质量。

## 持续优化闭环

```
Collect（数据/轨迹）→ Grade（多维度评估）→ Analyze（找瓶颈维度）→ Iterate（改进）
```

| 阶段 | 评估策略 | 理由 |
|------|---------|------|
| 快速迭代 | Direct + rule-based | 速度优先 |
| 候选版本验证 | Voting(3) + LLM grader | 可靠性 |
| 版本发布决策 | Auto Arena pairwise | 对比说服力 |
| 生产监控 | 采样 + Langfuse | 成本控制 |
| RL 训练 | VERL + mixed graders | 信号质量 |

## 关键陷阱

**VotingStrategy 用偶数票数**：偶数平票概率高，推荐奇数（5 票）。

**GRPO reward 不是绝对分数**：`GRPOTournamentEvaluationStrategy` 输出 `[-1, 1]` 相对胜率，同一响应在不同对手群体中 reward 不同。

**pairwise 不做双向比较就用胜率**：会因 LLM 位置偏好产生系统性偏差。

**LangSmith/Langfuse 集成代码在 Jupyter 中使用 asyncio.run() 报错**：需要 nest_asyncio。
