# Rubric 生成与自定义 Grader 构建方法

## 核心认知：三条路径的本质差异

OpenJudge 提供三种构建 grader 的路径，选择哪条不是技术问题，而是**你拥有什么数据**的问题：

| 路径 | 你拥有的数据 | 核心成本 | 适用场景 |
|------|------------|---------|---------|
| **FunctionGrader（Python 接口）** | 明确的规则/算法 | 工程实现 | 数学题、代码执行、格式验证等可确定性判断 |
| **SimpleRubricsGenerator** | 任务描述 + 少量样例查询 | LLM 调用（单次） | 新任务冷启动、快速原型、无标注数据 |
| **IterativeRubricsGenerator** | 已标注偏好数据（query+response+score/rank） | LLM 调用（多次迭代） | 有历史标注、需要高质量 rubric、复杂评估任务 |

**关键反直觉**：IterativeRubricsGenerator 不是"更好的"SimpleRubricsGenerator，它们解决的是完全不同的问题。Simple 是零样本的任务理解，Iterative 是从数据中归纳评估标准。

---

## FunctionGrader：最被低估的路径

函数 grader 接受任意 Python callable，签名为：
```python
async def my_func(query: str, response: str, **kwargs) -> GraderScore
```

适用场景比预期更广：
- **确定性判断**：答案唯一正确（数学、代码输出）
- **规则检查**：格式合规、字符数、关键词存在
- **混合策略**：先用函数过滤明显错误，再用 LLM 评估边界案例

陷阱：FunctionGrader 默认 mode=POINTWISE，listwise 需要显式指定，且返回类型需要对应切换为 GraderRank。

---

## SimpleRubricsGenerator：零样本生成的机制与局限

**流程**：task_description + scenario + sample_queries → LLM 单次调用 → 3-5 条 rubric → LLMGrader

关键配置决策：
- `task_description`：必填，越具体越好。"医疗问答系统" vs "评估放射科医生关于CT影像分析的回答质量" 效果差距巨大
- `scenario`：可选但重要，影响 rubric 的侧重点
- `sample_queries`：建议提供 3-5 条真实样例，帮助 LLM 理解查询类型
- `min_score`/`max_score`：设计分数空间，二元场景用 0-1，细粒度用 0-4

**内置降级机制**：生成失败时回退到默认 rubric（准确性、相关性、完整性），这是有意识的设计——宁可有一个通用 grader 也不要崩溃。max_retries=3 控制重试次数。

**SimpleRubricsGenerator 的根本局限**：生成的 rubric 基于任务描述的语言理解，不基于实际数据分布。如果你的任务描述和真实评估标准存在语义偏差（常见于专业领域），生成的 rubric 可能系统性偏错。

---

## IterativeRubricsGenerator：从数据中学习评估标准的两阶段机制

这是 OpenJudge 最核心的创新，基于论文 [Auto-Rubric](https://arxiv.org/abs/2510.17314)。

### 第一阶段：Query-Specific Rubric 生成（Propose-Evaluate-Revise 循环）

对每条训练样本执行：
1. **Propose**：基于 query+response+label 生成 N 条 rubric（N=query_specific_generate_number）
2. **Evaluate**：用生成的 rubric 对该样本重新评分/排序
3. **Validate**：比较评分结果与 label，判断是否正确
4. **Revise（如果失败）**：将失败原因作为 feedback，要求 LLM 改进 rubric
5. 循环直到验证通过或达到 max_epochs

**验证的隐性逻辑**：只有能正确预测训练样本标签的 rubric 才会被保留（rubric_valid=True）。这确保了每条 rubric 至少在生成它的样本上是有效的。

**被跳过的样本**：验证失败的样本（rubric_valid=False）生成的 rubric 直接丢弃，不进入第二阶段。这意味着数据中的高难度/模糊样本会被自然过滤。

### 第二阶段：MCR² 聚合与可选 Categorization

**采样模式自动切换**（关键！）：
- `≤100 样本`：ALL_SAMPLES 模式，并发处理所有样本，收集全部有效 rubric
- `>100 样本`：SMART_SAMPLING 模式，使用 MCR²（最大编码率减少）迭代选择信息量最大的 rubric 子集

**MCR² 的意义**：基于信息论，选择在嵌入空间中多样性最大的 rubric 集合。通过 Dashscope TextEmbedding API 生成向量，用 SVD 计算编码率。这是去除冗余 rubric 的数学严格方法，而不是简单的语义去重。

**注意**：MCR² 依赖 Dashscope API（阿里云），这是一个隐性的外部依赖，在非中国区或没有 Dashscope 账号时会静默返回零向量，导致选择退化。

**Categorization（可选，默认关闭）**：
- 关闭时：所有有效 rubric 以编号列表形式呈现
- 开启时：LLM 将散碎 rubric 聚合成 Theme-Tips 层级结构（主题+具体要点），目标类别数由 categories_number 控制（默认 5）

何时开启 categorization：rubric 数量大（>20），希望 LLM 在评估时有更清晰的结构。代价是一次额外 LLM 调用，且有聚合失败（自动降级到编号列表）的风险。

---

## 停止条件与迭代的实际设计

### Propose-Evaluate-Revise 的停止
- 验证通过 → 立即停止（这是好情况）
- 达到 max_epochs（默认 3-5）→ 使用最后一轮 rubric，但标记为 valid=False，然后丢弃

**重要**：max_epochs 是每个样本独立的限制，与全局 max_iterations（SMART_SAMPLING 的批次限制）完全不同。

### SMART_SAMPLING 的停止条件（三个并行）
1. **信息增益收敛**：连续 `patience`（默认 2）次迭代中，MCR² 编码率增量 < `min_increment_threshold`（默认 0.002）
2. **rubric 池上限**：选中 rubric 数达到 `max_total_rubrics`（默认 200）
3. **迭代次数上限**：达到 `max_iterations`（默认 50）

**实践建议**：对于 <500 样本的中等规模数据集，降低 patience=1 和 max_iterations=20 可显著减少成本，收益损失可忽略。信息增益曲线通常在前 5-10 次迭代就趋于平稳。

---

## min_score/max_score：分数空间的设计哲学

这个参数影响深远，远超"设置分数范围"这么简单：

**影响 rubric 生成方向**：生成提示中明确包含分数范围。0-1 二元分数促使 LLM 生成区分"有"和"没有"的标准；0-4 细粒度分数促使生成"程度"判断标准。

**影响验证严格程度**：验证时会比较预测分数与 label_score。0-1 空间的验证是严格的（二值正确/错误）；GRPO 训练文档中可见，对于 label 2-4 的验证允许比例误差（`reward = 1.0 - |predicted - true| / 4`）。

**实践建议**：
- 偏好/排序判断 → 0-1（binary）
- 质量评分（有细粒度区分需求）→ 0-4
- 避免 0-10 或 0-100，粒度过细会让 rubric 失去判别力

---

## 手写 Rubric vs 生成 Rubric：决策框架

**手写 rubric 优先的情况**：
- 存在监管要求或专业共识（医疗、法律、财务合规）
- 团队对评估标准已有清晰共识
- 数据量极少（<10 样本），生成 rubric 的统计基础不足
- 已经有人工验证过的历史评估标准

**生成 rubric 优先的情况**：
- 新任务快速建立基线
- 评估标准难以形式化但数据中隐含
- 需要跨语言评估（ZH/EN 模板均有）
- rubric 需要随数据分布演化

**混合策略（最实用）**：
1. 先用 SimpleRubricsGenerator 快速生成草稿 rubric
2. 人工审查并修改
3. 将修改后的 rubric 直接作为 LLMGrader 的 rubrics 参数（字符串格式）
4. 如果有标注数据，用 IterativeRubricsGenerator 生成候选，与手写版做对比验证

---

## 质量验证方法

OpenJudge 提供 `AccuracyGraderValidator`，接受测试集、grader 和字段映射，返回 AnalysisResult（包含准确率等指标）。

**验证的核心问题**：不是"这个 rubric 语义上合理吗"，而是"这个 grader 在测试集上的准确率多少"。

实践验证流程：
1. 将标注数据 80/20 分成 train/test
2. 用 train 生成 rubric（IterativeRubricsGenerator）或描述 rubric（SimpleRubricsGenerator）
3. 用 AccuracyGraderValidator 在 test 上评估
4. 对比不同策略的准确率

**注意**：rubric_valid 只是内部验证（rubric 能否重现训练样本的标签），不等于泛化准确率。内部验证通过的 rubric 仍可能在测试集上表现差（过拟合特定样本的表述风格）。

---

## 训练 Judge Model：适用门槛

三种训练范式及其门槛：

| 方式 | 最低数据量 | 基础设施 | 适用条件 |
|------|----------|---------|---------|
| **SFT（监督微调）** | ~数千对话 | verl + 多 GPU | 需要模型输出可解释的评分理由；有高质量示范数据 |
| **Bradley-Terry** | ~数千偏好对 | verl + 多 GPU | 有二元比较标注；只需要标量分数不需要解释 |
| **GRPO（强化学习）** | ~数千样本 | verl + Ray + 多 GPU | 需要推理能力（输出含 `<think>` 过程）；有验证奖励函数 |

**关键门槛判断**：如果数据量 <1000，或者没有 GPU 集群，不要训练 judge model，使用 LLMGrader + rubric 是正确选择。训练的意义在于把偏好知识烧入权重，而不是每次推理都需要通过 prompt 传递 rubric。

**GRPO 的特殊价值**：GRPO 训练的模型输出包含思维链（`<think>...</think><score>N</score>`），这既提高可解释性，也使奖励函数可以基于输出格式做规则验证（格式合规 → reward=1，否则 reward=0），降低了标注对精确数值的依赖。

---

## 隐性约束与踩坑点

1. **MCR² 的 Dashscope 依赖**：SMART_SAMPLING 模式隐式依赖 Dashscope TextEmbedding API。国际环境中调用失败会静默降级（返回零向量），导致 rubric 选择退化为随机。需要设置 `DASHSCOPE_API_KEY` 环境变量。

2. **验证通过率影响 rubric 质量**：如果数据标注质量差（标注者间一致性低），大量样本会验证失败，导致最终 rubric 池很小，泛化能力差。这是数据质量问题，不是参数调整能解决的。

3. **分数范围与 label 字段名**：IterativeRubricsGenerator pointwise 模式期望数据字段名为 `label_score`（不是 `score`），listwise 期望 `label_rank`。字段名不对会导致验证全部失败但不报错（因为 validate 函数找不到 label 时直接返回 False）。

4. **categorization 会改变 rubric 格式**：关闭时格式是 `1. criterion text`，开启时是 `Theme: ...\n- Tip1: ...\n- Tip2: ...`。如果后续代码依赖 rubric 格式解析，需要注意这个切换。

5. **LLMGrader 的 task_description_section**：这是一个模板占位符，在 SimpleRubricsGenerator 和 IterativeRubricsGenerator 中都会自动填充。手动构建 LLMGrader 时如果使用同一套模板，需要显式传 `task_description_section=""` 或对应内容，否则模板渲染会报错。
