# Agent 全生命周期评估、高级模式与外部集成

> 研究对象：workspace/OpenJudge/  
> 研究日期：2026-04-14

---

## 一、为什么传统 Grader 不够用于 Agent 评估

### 根本问题：单点 vs. 过程

传统 grader 评估一个 **输入→输出** 对。Agent 的本质是 **多轮决策序列**：每一步选择工具、形成观测、更新状态。只看最终答案等于只看棋局结果而不看棋局。

以下维度是传统 grader 无法捕捉的：

**Trajectory（轨迹整体质量）**  
- 路径效率：是否走了冤枉路？  
- 逻辑一贯性：中间推理是否自洽？  
- 目标对齐：每步是否朝任务方向前进？  
- 对应 grader：`TrajectoryComprehensiveGrader`，输入整条 messages 序列

**Tool Use（工具调用行为）**  
- 工具选择正确性：用了对的工具吗？  
- 参数质量：参数是否合理、完整？  
- 调用循环检测：是否陷入重复调用？（`ActionLoopDetectionGrader`）  
- 信息增益：每次调用是否带回新信息？（`ObservationInformationGainGrader`）

**Memory（记忆与上下文利用）**  
- 是否利用了之前观测到的信息？  
- 是否重复问了同样的问题？  
- 传统 grader 无法感知历史轨迹，因此无法评估

**Plan（规划质量）**  
- 初始任务分解是否合理？  
- 遇到障碍时重规划能力如何？  
- 这个维度往往需要人工审查或专用 LLM-as-Judge

### 关键决策原则

> **评估 Agent 必须把 messages 历史作为一等公民输入，而不是把最终答案剥离出来**。

这意味着 grader 的 `aevaluate` 接口需要接受 `messages: List[Dict]` 而非简单的 `query/response` 字段对。OpenJudge 中专为 agent 设计的 grader 全部遵循这个约定。

---

## 二、AgenticGrader：让 Grader 本身也成为 Agent

### 核心洞察

评估工具性事实（"这句话说的对吗？"）时，静态的 LLM judge 受限于知识截止日期和幻觉风险。AgenticGrader 的思路是：**让 grader 自己先用工具查，再做判断**。

这是 OpenJudge 中唯一一个把"评估者"也变成 agent 的 grader。

### 架构分层

```
AgenticGrader
  └── agent: BaseAgent
        ├── model: BaseChatModel
        ├── tools: Dict[str, BaseTool]
        └── max_iterations: int
```

三层分工明确：
- `AgenticGrader`：负责模板渲染、结果解析、grader 协议封装
- `BaseAgent / ReActAgent`：负责推理循环（ReAct pattern）
- `BaseTool`：负责具体能力（搜索、代码执行等）

### 关键设计决策："预构建 agent，传入 grader"

```python
# 正确做法
agent = ReActAgent(model=..., tools=[...])
grader = AgenticGrader(agent=agent, template="...")

# 错误直觉：想在 grader 里配置 model/tools
# AgenticGrader 不接受这些参数 —— 必须先建好 agent
```

这个"先建 agent 再传入"的设计强制关注点分离。从配置文件加载时可以用 `AgenticGrader.from_config()`，它内部会自动创建 `ReActAgent`。

### 输出解析的鲁棒性

AgenticGrader 内置两级 fallback 解析：
1. 正则匹配 JSON（支持嵌套 JSON）
2. 正则匹配 score/rank 文本模式

这意味着 template 里要求 JSON 输出时，即使 agent 多加了文字说明，也能正确提取分数。

---

## 三、工具生态与适配器模式

### 四种接入工具的方式（按依赖复杂度递增）

**1. 原生 BaseTool 子类**（零依赖）  
实现 `schema` class attribute 和 `aexecute` 方法。最轻量，推荐用于生产环境。

**2. FunctionToolAdapter**（轻量包装）  
把普通 Python 函数直接包为 tool，适合快速原型：
```python
tool = FunctionToolAdapter(func=my_fn, name="...", description="...", parameters={...})
```

**3. LangChainToolAdapter**（工具层适配）  
保留 OpenJudge 的 ReActAgent，只把 LangChain 的 tool 对象包装进来。适合想用 LangChain 庞大工具库但不想引入 LangChain agent 依赖的场景。

**4. LangChainAgentAdapter / AgentScopeAgentAdapter**（整体 agent 适配）  
把整个第三方 agent 包装为 OpenJudge `BaseAgent`，完全委托推理逻辑。适合已有成熟 LangChain agent 的团队。

### 选择哪种方式的决策树

```
已有 LangChain agent，不想重写？
  → LangChainAgentAdapter（整体委托）

已有 LangChain tool，想用 OpenJudge 推理？
  → LangChainToolAdapter（工具层适配）

需要快速包装一个 Python 函数？
  → FunctionToolAdapter

追求零依赖、生产稳定性？
  → 原生 BaseTool 子类
```

### 隐性约束：适配器在 cookbook，不在核心库

`LangChainAgentAdapter`、`AgentScopeAgentAdapter` 不在 `openjudge.agentic` 核心路径里，而在 `cookbooks/agentic_grader/adapters/`。这是刻意的——避免把可选依赖引入核心包。使用时需自行复制或安装对应依赖。

---

## 四、Auto Arena / Pairwise 评估的设计思想

### 核心问题：没有标注数据时如何评估？

传统评估需要 ground truth。Auto Arena 的答案是：**让模型互相比较，用胜率作为排名依据**，完全不需要标注。

### 五步 Pipeline

```
1. 生成查询（QueryGenerator）    → 多样性、难度覆盖
2. 收集各模型响应（ResponseCollector）
3. 生成评测 rubric（TaskBasedRubricGenerator）
4. 全配对 + 双向比较（LLMGrader, POINTWISE）
5. 计算胜率矩阵（PairwiseAnalyzer）
```

### 关键设计细节

**双向比较消除位置偏差**  
每对 (A, B) 都比较两次：原顺序 + 交换顺序。只有两次结果一致时才算有效胜负（`GRPOTournamentEvaluationStrategy` 的 `debiased=True` 模式）。

**胜率矩阵 vs. 单一排名**  
输出是 N×N 矩阵，可以看到"A 在 X 类任务上强，B 在 Y 类任务上强"的细粒度信息，而非简单线性排名。

**N*(N-1)/2 规模问题**  
5 个模型 → 10 对 → 20 次比较（双向）。模型数量翻倍，比较次数四倍增长。实践中建议控制在 5-8 个模型以内，或用分层淘汰赛。

### 适用场景

- 模型版本迭代对比（A/B test 替代品）
- 无标注数据的新领域评估
- 主观任务（创意写作、对话质量）
- 需要向业务方展示"哪个更好"的说服性评估

### 不适用场景

- 需要绝对分数（"达到 80 分"）的场景
- 有明确正确答案的任务（用 CorrectnessGrader 更合适）
- 模型数量超过 10 个（比较次数爆炸）

---

## 五、Evaluation Strategy：同一 Grader，不同可靠性保证

### 设计洞察

LLM judge 具有固有随机性。相同输入可能得到不同分数。EvaluationStrategy 是一个**包装层**，在不改变任何 grader 代码的前提下，通过多次调用来提升结果可靠性。

### 四种策略对比

| 策略 | 调用次数 | 适用场景 | 核心逻辑 |
|------|---------|---------|---------|
| `DirectEvaluationStrategy` | 1 | 快速迭代、探索阶段 | 直接返回结果 |
| `AverageEvaluationStrategy` | N | 连续分数降噪 | 并行 N 次，取均值 |
| `VotingEvaluationStrategy` | N | 离散分数稳定化 | 并行 N 次，取众数 |
| `GRPOTournamentEvaluationStrategy` | N*(N-1)/2 | RL 训练 reward | 全配对锦标赛，输出相对胜率 |

### 关键技术细节

- 所有 N 次调用是**并发执行**（`asyncio.gather`），不是串行。5 次调用不等于 5 倍延迟，取决于 API 并发限制。
- `VotingEvaluationStrategy` 推荐奇数 `num_votes`（3、5、7）以避免平票。平票时有 `MIN/MAX/CLOSEST_TO_MEAN` 三种 tiebreaker。
- `GRPOTournamentEvaluationStrategy` 输出的是 `[-1.0, 1.0]` 范围的**相对胜率**，不是绝对分数。这是专为 GRPO 训练设计的 reward shape。

### 什么时候切换策略

- 评估结果方差大 → 切换到 Voting 或 Average（提高可靠性，代价是成本增加 N 倍）
- 用于 RL 训练 → 必须用 GRPOTournament（需要组内相对排名）
- 生产环境低延迟 → 保留 Direct，但可以在 Direct 基础上加缓存

---

## 六、LangSmith 集成：与原生 Runner 的核心区别

### 数据流方向不同

**原生 Runner 模式**：OpenJudge 主导，主动拉数据→评估→存本地
```
dataset → GradingRunner → results (本地文件/内存)
```

**LangSmith 集成**：LangSmith 主导 evaluation loop，OpenJudge 作为 evaluator 插件
```
LangSmith dataset → evaluate() → [OpenJudge evaluator] → LangSmith 存储分数
```

### 关键适配层

LangSmith evaluator 是同步函数，签名固定：
```python
def evaluator(inputs: dict, outputs: dict, reference_outputs: dict) -> dict
```

而 OpenJudge grader 是异步的 `aevaluate`。因此需要用 `asyncio.run()` 桥接：
```python
result = asyncio.run(grader.aevaluate(**mapped_data))
```

**注意陷阱**：在已有 event loop 的环境（如 Jupyter、某些 async 框架）中，`asyncio.run()` 会报错。此时需要用 `nest_asyncio` 或改用线程池。

### Mapper 的作用

LangSmith 的数据格式是 `{inputs: {...}, outputs: {...}, reference_outputs: {...}}`，而 OpenJudge grader 期望 `query=..., response=...`。Mapper 是一个字段路径映射字典：

```python
mapper = {
    "query": "inputs.question",        # 从 inputs.question 取值
    "response": "outputs.answer",      # 从 outputs.answer 取值
}
```

这个映射是 LangSmith 集成的核心胶水代码，每个 grader 可以有不同的 mapper。

### 何时选 GradingRunner 方式（批量）vs. 单个 grader

- 单 grader：适合需要细粒度控制、只关心一个维度的场景
- GradingRunner：多维度评估，利用并发降低总耗时；但需要在 `__call__` 中处理结果格式转换

---

## 七、Langfuse 集成：Trace-based 而非 Dataset-based

### 与 LangSmith 的关键区别

LangSmith 是以 **dataset** 为中心（预先定义输入输出对），而 Langfuse 是以 **trace** 为中心（从线上运行记录中取数据）。

**Langfuse 数据流**：
```
线上运行（带 @observe 装饰器）→ Langfuse 存 trace
                                    ↓
                         langfuse.api.trace.list()
                                    ↓
                    OpenJudge grader.aevaluate()
                                    ↓
                    langfuse.create_score(trace_id=...)
```

这意味着 Langfuse 集成天然支持**持续监控**和**历史数据离线评估**——无需重新运行应用，直接对历史 trace 打分。

### 适用场景对比

| 场景 | LangSmith | Langfuse |
|------|-----------|----------|
| 开发阶段 benchmark | 更适合（dataset 驱动） | 可用 |
| 生产监控 | 可用 | 更适合（trace 驱动） |
| 历史数据回溯评估 | 需要重新运行 | 直接对 trace 评估 |
| 增量评估（只评新数据） | 手动实现 | 天然支持（time range filter） |

---

## 八、VERL 集成：从评估到 RL 训练的闭环

### 问题背景

VERL 的 RL 训练需要 reward signal。对复杂 agent 任务（金融分析、代码生成），单一 reward 维度（如"是否完成任务"）无法提供足够信号。OpenJudge 提供多维度、混合规则+LLM 的 reward 计算。

### 三层架构

```
VERL 训练框架
    └── RewardManager（框架适配层）
            ├── token decoding（VERL DataProto → text）
            ├── prompt grouping（按 prompt 分组 rollouts）
            └── tensor filling（结果→VERL reward tensor）
                    └── RewardFunction（业务逻辑层）
                            └── GradingRunner + Graders（评估层）
```

### 关键技术点

**Prompt Grouping 的重要性**  
VERL 每个 prompt 生成 N 个 rollout（通常 N=4）。把同一 prompt 的响应分组后，才能做 listwise 比较和组内相对评分——这是 GRPO 算法所需要的。

**Fresh Instance Pattern**  
每次调用 `compute_batch_scores` 时都要创建新的 `GradingRunner` 实例，不能复用。原因是 async event loop 在不同调用间可能变化。

**混合 rule-based + LLM grader 的意义**  
- Rule-based grader（如 ActionLoopDetection）：零延迟、零成本，提供硬约束
- LLM grader（如 TrajectoryComprehensive）：语义理解，但成本高

生产中的推荐配比：rule-based 负责惩罚明显错误（loop、信息无增益），LLM 负责评估整体质量。

### 门槛与适用条件

适合使用 VERL + OpenJudge 的条件：
- 任务复杂，单一 reward 维度信号不足
- 已有 VERL 训练基础设施
- 愿意为 LLM judge 的 API 调用付出成本

**不适合**：简单任务（数学、代码执行有明确正误）、资源受限（每个 rollout 都调 LLM judge 成本很高）

---

## 九、持续优化闭环设计

### 完整闭环模型

```
Collect → Grade → Analyze → Iterate
```

**Collect（数据收集）**  
- 直接运行 agent，收集 messages 历史（trajectory）
- 线上系统：用 Langfuse @observe 自动捕获
- 离线：用 Auto Arena 的 ResponseCollector

**Grade（评估打分）**  
- 多维度：trajectory + tool_use + output_quality
- 策略选择：探索阶段用 Direct，生产稳定性用 Voting
- 批量：GradingRunner 控制并发

**Analyze（分析）**  
- Pairwise 分析：哪个 agent 版本更好？（PairwiseAnalyzer）
- 维度拆解：哪个维度是瓶颈？
- Langfuse/LangSmith 可视化：trend over time

**Iterate（迭代）**  
- 如果是 RL 训练：直接用 VERL + OpenJudge reward 迭代
- 如果是 prompt 工程：用评估结果定向改进

### 关键洞察：评估频率 vs. 评估深度的权衡

| 阶段 | 建议评估策略 | 理由 |
|------|------------|------|
| 快速迭代 | Direct + rule-based | 速度优先 |
| 候选版本验证 | Voting(3) + LLM grader | 可靠性 |
| 版本发布决策 | Auto Arena（pairwise） | 对比说服力 |
| 生产监控 | 采样 + Langfuse | 成本控制 |
| RL 训练 | VERL + mixed graders | 信号质量 |

---

## 十、常见陷阱与纠偏

### 陷阱 1：把 AgenticGrader 的 template 当 agent 的 system prompt

AgenticGrader 的 template 同时提供了 system prompt 和 user 消息。不需要（也不应该）在 ReActAgent 初始化时再设置 system prompt——那会导致消息混乱。

### 陷阱 2：忘记双向比较就用胜率排名

Pairwise 评估如果只做单向比较（A vs B），会因 LLM 的位置偏好（更倾向第一个响应）导致系统性偏差。双向比较或 `debiased=True` 是必须的。

### 陷阱 3：在有 event loop 的环境用 asyncio.run()

LangSmith 和 Langfuse 的集成代码都用了 `asyncio.run()`，这在 Jupyter、FastAPI 等环境会报错。需要用 `nest_asyncio.apply()` 或在线程池里运行。

### 陷阱 4：VotingStrategy 用偶数票数

偶数票平票概率高。3 票 → 1 票概率 25%，5 票 → 约 31%，但奇数 5 票可以避免 50% 的 2:2 局面。实践中推荐 5 票。

### 陷阱 5：把 GRPO 的 reward 理解为绝对分数

`GRPOTournamentEvaluationStrategy` 输出的是 `[-1, 1]` 的相对胜率，不是质量绝对分数。同一个响应在不同对手群体中会有不同 reward——这是设计意图，GRPO 需要组内相对排序，而非绝对质量。

### 陷阱 6：LangChain Adapter 在 cookbook，不在核心包

初学者容易在 `openjudge.agentic` 里找适配器，找不到。适配器刻意放在 `cookbooks/agentic_grader/adapters/`，需要手动复制到项目中使用。
