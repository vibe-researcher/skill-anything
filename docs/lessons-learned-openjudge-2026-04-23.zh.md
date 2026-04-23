# 经验教训 — OpenJudge 蒸馏运行（2026-04-23）

> 审阅者：在 v2 Markov-Orchestrator 框架上完成 iter-0 → iter-2（目标：OpenJudge）之后的系统性反思。
> 最终状态：composite=0.55，quality=4.58/5，skill_won_rate=12/12（100%），
> 但**每个任务的 composite 均回归 ≥ -0.189**，原因是轨迹效率信号崩溃。
> 本文档是根因分析与改进计划。

## TL;DR

- **Composite 中的轨迹分量是整个循环中最脆弱的输入。** 它悄无声息地吞噬了 iter-2 的所有质量提升，并且会机械性地触发对*更好*版本 Skill 的回滚。
- **评分流水线周围到处都是静默失败点**：schema 不匹配的 Grader 输出 → `composite=0`；被复制进 worktree 的 `blind-mapping.json` → 几乎破坏盲测；中文理由中 token 零重叠 → 大量误报过拟合告警。
- **循环没有设计任何对评测集饱和的响应机制。** skill_won_rate 达到 100% 后没有新任务生成路径，意味着每次后续迭代都必然产生噪声而非进展。

## 背景

本次运行使用 `SKILL.md` v2（Markov 编排器、物理隔离、OSR 协议、Grader 集成 K=3 可用但因成本选用单 Grader）将 OpenJudge 仓库跑了三轮迭代。我在**Super-Runner 模式**下执行了 Runner（单次 Agent 启动处理全部 12 个任务 × 2 个变体，而非 24 次独立启动）——这是一种系统未正式承认的有意识成本权衡。运行完成；产出的 Skill 在每个任务的质量分上客观优于 iter-1；然而机器裁决报告每个任务均出现回归。本次审查梳理了导致这一分歧的摩擦点，并提出具体的修复方案。

---

## P0 — 必须修复（阻塞正确性或安全性）

### P0.1 `worktree_helper --include` 会将盲测真实标签批量复制进 Grader worktree

**现象**：使用 `--include evals/results/iter-1` 创建 Grader worktree 时，helper 的 `cp -R` 复制了整个目录——其中包含 `blind-mapping.json` 和原始的 `eval-results.json`。我不得不在启动 Grader 前手动 `rm` 它们。

**根因**：`scripts/worktree_helper.py:_cp_tree`（第 55-67 行）对 `--include` 中的每个路径做普通递归复制。`--exclude-guard` 机制（第 89、124-130 行）仅**事后验证**顶层目录名的缺失——不会过滤被包含目录树中的文件。`blind-mapping.json` 和 `eval-results.json` 存在于被包含的目录内，因此任何守卫都不会触发。

**风险**：**这是整个循环中最危险的 bug。** 如果人工编排者未注意到，每个 Grader 的 CWD 中都会出现真实标签映射，盲测实际上被破坏，composite 分数将失去任何意义。Grader OSR 中的 `blind_discipline_check` 字段是自报告的，无法检测此问题——Agent 可以在不留任何痕迹的情况下读取该文件。`SKILL.md:111` 和 `references/eval-loop.md:115,126` 都明确写有"绝不拷贝 blind-mapping.json"，但执行依靠备忘录，而非代码。

**修复**（按优先顺序）：
1. **为 `worktree_helper.py` 添加 `grader` 用途预设**。当 `--purpose` 以 `grader-` 开头时，自动将以下路径追加到内部黑名单（无论 `--include` 包含什么）：`blind-mapping.json`、`eval-results.json`、`skills/**`、`state/**`、`grader-scores.json`（上一轮的分数也属于泄漏）。在 `_cp_tree` 内部做后置过滤，或在复制后做一次 `find -delete`。
2. **添加 `--exclude-path` 标志**（与 `--exclude-guard` 不同），接受 glob 模式，在后置验证步骤之前从已复制的目录树中删除匹配条目。在 SKILL.md §5.2 中对两者均加以说明。
3. **扩展第 124 行的后置验证**：递归遍历整个 work_path，检查关键泄漏集合 `{blind-mapping.json, eval-results.json}` 中的文件名，若发现则大声报错。这是对任何未来 `--include` 误用的最后防线。
4. **更新** `references/eval-loop.md` 的 Grade 部分，指示编排者使用新的预设/标志，而非记忆性的 `rm` 命令。

预计工作量：1-2 小时。**最先修复此项。**

---

### P0.2 `deblind_and_score.py` 在 Grader 输出 shape 不匹配时静默返回 `composite=0, per_task=[]`

**现象**：首次 deblind 尝试输出了 `composite_score=0`、空 `per_task`，且 `grader-scores.json` 仅 2 字节（`[]`）。无警告、无 stderr。原因：Grader OSR 使用 snake_case（`task_id`、`quality_a`、`winner`），遵循 `agents/grader.md:77-89`，而 `scripts/deblind_and_score.py:72-93` 读取 camelCase（`taskId`、`outputA.quality`、`toolUseCount`）。

**根因**：`scripts/deblind_and_score.py:64-65` 允许多种顶层 shape（list vs `.tasks` vs `.scores`），而 `mapping_by_task` 使用 `m["taskId"]`——当 shape 不匹配时，这在实践中会引发 KeyError，但被迭代循环静默吞噬，因为 shape 不匹配时没有任何条目产出，`scores_list` 为空。没有 shape 断言，没有警告，也没有非零退出码。

Agent 契约（`agents/grader.md:77`）规定 `per_task[].task_id` / `quality_a` / `quality_b` / `winner`——**编排者应当将其转换为 scorer 可接受的 `blind-grader-scores.json` shape，但整个代码库中没有任何脚本负责这一转换**。我临时写了一个 shim，但它未入库。

**风险**：静默的 `composite=0` 对收敛检查和任何 Markov 续跑来说都意味着"本轮迭代零改善"。一个合理的失败模式：编排者运行 deblind，看到 composite=0，判断"Skill Writer 坏了"，触发 `osr_rejected` + 重新启动，耗尽整个迭代预算，而根本原因是评分 bug。

**修复**：
1. **在 `deblind_and_score.py` 第 64 行之前添加显式 shape 检测**：若顶层为 dict，先查 `per_task` 键（新的首选 shape），再查 `tasks`/`scores`（旧版）。若列表为空且输入 payload 非空，则抛出带有期望顶层键的明确错误信息。
2. **原生支持两种 schema**：对每个评分条目，同时接受 `task_id` 或 `taskId`；`quality_a`+`quality_b`+`winner` 或 `outputA/outputB.quality`。在循环顶部归一化为单一内部 shape。
3. **编写 `scripts/grader_to_scorer_shim.py`**，接受一个或多个 Grader-OSR 文件并生成旧版 `blind-grader-scores.json`。在 `references/eval-loop.md` Score 部分引用此脚本。
4. **当 `len(scores_list) == 0` 时以非零状态退出**，并输出详细 stderr——零长度评分从来不是健康结果。
5. **收紧 `agents/grader.md`**，添加明确的"下游 scorer 期望的确切 shape"说明块，或确定唯一 shape 并重写 scorer。

预计工作量：2-3 小时。

---

### P0.3 Composite 公式在 1:1 工具调用比例附近导致轨迹信号崩溃

**现象**：iter-1 工具调用数（super-runner 聚合）：14（有 Skill）/ 32（无 Skill）→ `trajectory = max(0, 1 - 14/32) = 0.5625`。iter-2：19/18 → `max(0, 1 - 19/18) = 0`（被截断到 0）。单此翻转就造成每个任务 40% × 0.5625 ≈ **-0.225 composite**——这与所有回归任务观察到的 delta 完全吻合（`iter-2/iteration-summary.json` 显示 -0.189 到 -0.237）。质量实际上在每个任务上均有提升。

**根因**：`scripts/deblind_and_score.py` 中两个叠加问题。
1. **公式脆弱性**（第 28-31 行）：`max(0, 1 - with/without)` 是一个硬单侧比率。在 1:1 附近，它（a）不对称——降低 10% 成本对分数几乎没有影响，增加 10% 成本则直接归零——以及（b）在奇偶附近有很大的灵敏度区间，微小的测量噪声会主导真实信号。有 Skill 的运行中多几次工具调用，整个迭代就变成"回归"。
2. **每任务工具调用数仅在每个任务独立 Runner 启动时才是合理的每任务值。** Super-Runner 模式产生*会话聚合*工具数；没有合理的方式将其分配到各任务。每个任务的轨迹分实际上是同一个会话级数字，这使得 40% 的轨迹权重成为噪声放大器。

**风险**：如前所述，**机械执行"composite 回归 > 0.05 → 回滚"规则（SKILL.md:224）将会回滚客观上更好的 iter-2 Skill。** 这是自我改进循环的正确性失效。

**修复**——需要多项措施，因为单项变更不够：
1. **替换 `compute_trajectory` 中的轨迹公式**，使用有界、对称、饱和的形式。建议：
   ```python
   def compute_trajectory(tool_calls_with, tool_calls_without):
       if tool_calls_without <= 0 or tool_calls_with is None:
           return None  # 不可用，而非 0
       # 对称 log 比率压缩到 [-0.5, 0.5]，再平移到 [0, 1]
       import math
       ratio = tool_calls_with / max(1, tool_calls_without)
       raw = -math.log2(ratio) / 4   # 减少 2x → +0.25；增加 2x → -0.25
       return max(0.0, min(1.0, 0.5 + raw))
   ```
   这样奇偶点位于 0.5（而非 0），>1 的值不会截断为 0，在 n≈20 时 1 次工具调用差异使分数移动约 0.02（而非 0.56）。
2. **当 `tool_calls_without <= 0` 或日志值为 null 时返回 `None`，而非 `0`。** 将 None 传播到 composite 中作为"无轨迹分量可用"，并对该任务将质量重新加权至 100%。添加 `trajectory_available_ratio` 聚合字段，让编排者了解每个 composite 中有多少是纯质量分。
3. **检测 Super-Runner 聚合**：如果某轮迭代中每个任务的 with/without 对相同，则在 `guardrail_flags` 中标记 `trajectory_aggregated, severity=warn`，并将所有任务的轨迹设为 `None`（依赖纯质量）。检测成本低：若唯一的 `(with, without)` 对数量为 1 且任务数 > 1，即可判定。
4. **拆分回归守卫**（SKILL.md:224）。将"质量回归 > 0.05"与"composite 回归 > 0.05"分开——只有前者应触发自动回滚。质量提升但 composite 下降时，应追加 `warn` 并要求编排者确认（或新 OSR 事件 `trajectory_regression_observed`），而不回滚。

预计工作量：3-4 小时（公式 + 单元测试 + 守卫拆分）。

---

## P1 — 高价值

### P1.1 Super-Runner 模式下没有可用的权威轨迹测量来源

**现象**：`scripts/subagent_log.py count-by-uuid` 对每个异步 Task 启动都返回 `source=not_available, reason=async_not_in_log`——而这正是我们实际启动 Runner 的方式。内存索引 `project_openjudge_distillation.md`（iter-1 记录）也注意到了相同的问题。编排者因此使用 Runner 的自报告，而在 Super-Runner 模式下这是一个会话聚合值，而非每任务数。

**根因**：`scripts/subagent_log.py:148-159` 在缺少 `isSidechain` 条目时正确报告 `not_available`——但这不是脚本的 bug，而是反映了**异步 Agent Task 调用确实不会发出 sidechain 记录**这一现实。`references/eval-loop.md:67-77` 中的整个权威工具调用数路径仅适用于同步子 Agent，而编排者很少使用这种模式。

`scripts/invariant_check.check_task_reality`（第 131-178 行）验证 `subagent_log_path` 存在且非空，但该字段在 OSR schema 中是一个**字符串**，没有最小长度要求——编排者可以（实际上也确实）写入 `""` 并通过验证。

**风险**：整个 P03 防线（"toolUseCount 伪造"）依赖统计手段（`tool_count_variance` 检查），因为直接验证路径实际上不可用。规范设计时这是可接受的，因为原意是每任务一个 Runner；但这种模式的成本/延迟迫使每次真实运行进入 Super-Runner 模式，整个层因此失效。

**修复**：
1. **将 Super-Runner 模式正式化为 `SKILL.md` §5.1 中的一等降级模式。** 明确记录权衡："N 任务单次启动失去每任务轨迹；轨迹分量自动禁用；composite 变为质量独占，并在 digest 中带有 `degraded_mode=super_runner` 标志。"
2. **在 `references/eval-loop.md` 中添加 Super-Runner 协议**：单个 Runner 必须*在自己的 OSR 中*发出 `per_task_tool_counts` 部分（非会话总计），承认这是自报告，但至少是按任务归属的。这样即使在聚合启动中，scorer 也有诚实的每任务数字。
3. **收紧 OSR runner schema**：要求 `subagent_log_path` `minLength: 1`，并在 `osr_validate.py` 中将路径存在性检查作为语义不变量（而非仅 schema 验证）。
4. **备选同步模式**：研究 Runner 步骤是否可以专门使用同步 Agent 调用（确实会发出 sidechain 条目），接受更高延迟作为轨迹正确性的代价。这是最干净的长期修复。

预计工作量：1 天（方案 1-3），加方案 4 则需最多 2 天。

---

### P1.2 `overfit_check.py` 的 token Jaccard 仅支持英文正则，中文理由上失效

**现象**：iter-1 Skill Writer 的 23 个 `changes_applied` 条目中有 22 个被标记为 `low_knowledge_overlap`（重叠 0.02-0.12，低于最低阈值 0.15）。我逐一核实，每个条目都确实基于 `knowledge/*.md` 中的引用。Skill Writer 本身也将根因记录为 `meta_observation`："token 正则仅支持英文"。

**根因**：`scripts/overfit_check.py:37`：
```python
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|\w+")
```
Python 3 中 `\w+` 确实包含 CJK 字符（Unicode 感知），但在实践中，中文文本没有空格，`\w+` 会将整句话识别为单个 token，约 5 个巨型"token"的 Jaccard 集合与同样退化分词的理由之间几乎零重叠。英文路径 `[A-Za-z_]...` 对 ASCII 文本短路，但对中英混合文本，分词由退化的 CJK 路径主导。

**风险**：每个以中文为主的 Skill Writer 迭代都会产生 20+ 个误报告警，编排者提示词训练它将此视为"走形式的引用"。这要么（a）被忽视，训练编排者忽视过拟合告警，要么（b）触发不必要的重新启动。两种结果都削弱了 P04 防线。

**修复**：
1. **对 CJK 进行适当分词**。两个简洁方案：
   - (a) 对 CJK 做字符级分词：将每个汉字视为一个 token。用两步替换 `TOKEN_RE`：
     ```python
     ASCII_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
     CJK_CHAR = re.compile(r"[一-鿿]")
     def _tokens(text):
         t = text.lower()
         return set(ASCII_WORD.findall(t)) | set(CJK_CHAR.findall(t))
     ```
     字符级重叠有噪声但可预期。
   - (b) 在 try/except 后加入可选 `jieba` 依赖，用于正确的中文分词；若未安装则回退到 (a)。
2. **当两侧均以非 ASCII 字符为主时，将阈值降低到 0.10**。在运行时自动检测。
3. **添加 `--tokenizer {ascii,cjk-char,jieba}` CLI 标志**，方便 A/B 对比信号。
4. **将重叠分布输出到 JSON**，让运维人员看到完整分布（而非仅通过/失败），便于校准。

预计工作量：2 小时。

---

### P1.3 OSR schema 的 `maxLength` 对诚实填写过于严苛；拒绝循环浪费迭代

**现象**：Researcher OSR 触发了 4 个 `maxLength` 违规：`surprises[].short`（120）、`surprises[].suggested_action`（200）、`research_direction`（200）、`meta_observations[]`（200）。我手动截断而非重新启动。

**根因**：`schemas/osr-researcher.schema.json:39,40,60,96` 和 `scripts/osr_validate.py:143-148` 将 `maxLength` 视为硬性拒绝，仅提供"截断至 N 字符"的建议文本，无自动恢复。`SKILL.md:253` 还规定拒绝最多重试 2 次，因此一个"话多"的 Researcher 可能仅因字符串长度而失败进入放弃流程——尽管内容完全正确。

**风险**：研究阶段频繁的误拒，降低了拒绝信号的价值（拒绝变成"噪声"而非"真正的 schema 违规"），且手动截断决策未被审计。

**修复**：
1. **对自由文本字段的 `maxLength` 超出自动截断并警告。** 在 `osr_validate.py` 中添加 `--auto-truncate` 标志（默认开启），就地重写字符串字段，添加 `...[截断，原始长度=N]` 标记，对每处发出警告，若截断是唯一问题则以 0 退出。在输出中添加 `truncations_applied` 字段供审计。
2. **提高内容密集字段的上限**：`research_direction` 300，`meta_observations[]` 300，`surprises.suggested_action` 300。这些大小是软性人体工程学限制，而非上下文预算限制——真正的预算检查在 `state.json`，不在这里。
3. **对结构性违规保持硬拒**（缺少必填字段、枚举不匹配、类型不匹配）。只有字符串长度才自动修复。

预计工作量：2 小时。

---

### P1.4 没有对评测集饱和（100% skill-won）的自动响应

**现象**：到 iter-2 时，所有 12 个任务报告 `skillWon=yes`，平均质量 4.58/5。`invariant_check.check_skill_won_rate`（`scripts/invariant_check.py:263-292`）标记了 `skill_won_rate=1.0 > 0.9`，建议"将 context_mode 升级为 'rich' 并读取 feedback_file"——但读取反馈只是确认了显而易见的事实（"Skill 赢得了每个任务"）。对饱和没有任何设计好的应对行动。

**根因**：守卫存在，但指向死胡同（读取反馈）。SKILL.md 的 `next_action` 映射（§4）没有"评测集饱和"的条目。Eval Designer 仅在 `generate` 阶段**启动一次**，在 `iterate` 期间从不重新访问。

**风险**：饱和后的每次进一步迭代都是浪费预算。更糟的是，饱和的评测集可能掩盖正交维度上的回归：当每个任务已经得 4+ 分时，迭代摘要无法区分"Skill 仍在能力 X 上改进"和"Skill 在不改变实质的情况下改写措辞"。

**修复**：
1. **在 `invariant_check.py` 中添加新的守卫响应**：当 `skill_won_rate >= 0.9` 且 `|Δ_quality_vs_prev| < 0.05` 且 `iteration >= 2` 时，将严重级别升至 `critical`，建议 `spawn_eval_designer_extension`。
2. **在 SKILL.md §4 中添加"评测扩展"流程**：新的阶段转换 `iterate → eval-extend → iterate`，使用特定提示词重新启动 Eval Designer："你正在使用 4-6 个任务扩展现有的 eval-tasks.json，这些任务应*压力测试*当前的 Skill——即你的目标是找到 Skill 尚未提供价值的任务。读取 `iteration-summary.json` 了解 Skill 已处理的内容。禁止：复制现有任务类别。"
3. **添加扩展模式任务 ID 前缀**（如 `ext-2-<slug>`），使跨轮迭代比较可以区分"原始集合"和"困难集合"指标。旧的 composite 平均值保持可比；新的困难集合平均值显示真实梯度。
4. **更新 `agents/eval-designer.md`**，添加"扩展模式"部分，继承所有隔离规则并添加禁止重复规则。

预计工作量：4-5 小时。

---

### P1.5 Eval Designer 在看到它要评分的 Skill 之前运行：先有鸡还是先有蛋

**现象**（我的观察，并非严格属于本次运行）：Eval Designer 在 `generate` 阶段启动，与 Skill Writer 并行，仅能访问 `knowledge/*.md`。它在 Skill 存在之前就编写任务，因此对"有 Skill 与无 Skill 的差距"的猜测都是推测性的。

**根因**：`SKILL.md:65-66` 将 Eval Designer 放在 `generate` 中——研究之后、迭代之前，但在实践中几乎与初始 Skill Writer 调用并行。任务设计依赖"阅读此 Skill 能实现什么，而仅阅读仓库无法实现什么"——这需要看到实际的 Skill。

**风险**：Designer 产生的任务（a）两个 Runner 都能从仓库独立回答（gap 低），或（b）两个 Runner 都无法回答（过度范围）。iter-0 Grader 反馈因此含糊，第一轮 Skill Writer 在低信号证据上运作。

**修复**：
1. **两阶段评测设计**：(a) `generate-pass-1` Eval Designer 仅从知识编写"推测性"任务。(b) iter-1 Grader digest 出来后，启动 `generate-pass-2` Eval Designer，提示词为："对于每个任务，我们现在有了 Skill + Runner 输出 + Grader 反馈。修订 quality_range < 1.5（区分度低）、quality_a=quality_b=5（双满分，浪费位置）或 suggestion_file 注明'模糊'的任务。"修订限于 diff，而非重写——大多数任务应保持不变。
2. **在 Eval Designer OSR schema 中添加 `task_revisions_applied` 字段**，让编排者可以审计跨阶段的变更。
3. **备选方案（成本更低）**：将第二阶段重命名为 `eval-repair`，仅在 iter-1 digest 中 `mean_quality_range < 1.0` 或 `zero_discrimination_tasks > 0` 时触发。

预计工作量：1 天。

---

### P1.6 机械回滚策略不区分质量回归和轨迹回归

**现象**（P0.3 已引用）：`SKILL.md:224`：按字面执行 `composite 回归 > 0.05 → 回滚`，将会恢复 iter-2 客观上更好的 Skill。

**根因**：`SKILL.md` 只描述了一个回归维度。`deblind_and_score.py:120-139` 只从 composite 计算 `delta`（第 136-139 行）。质量 delta 是可用的（在 per_task.quality 中），但未作为回归轴暴露出来。

**风险**：系统性偏向于不惜一切代价提高质量的迭代，但对稍微多用工具调用的迭代有偏见。长期来看，这会迫使 Skill Writer 追求简洁而不顾质量，与 `agents/skill-writer.md:11-19` 中质量优先的理念相冲突。

**修复**：
1. **在 `deblind_and_score.py` 中计算两个 delta 序列**：`composite_delta`（现有）和 `quality_delta`。在 `iteration-summary.regressions` 中作为独立数组输出两者：`composite_regressions`、`quality_regressions`。
2. **重写 SKILL.md §9 回滚行**：
   > composite 回归 > 0.05 但质量未回归 → 追加 `trajectory_regression_observed` 事件，**不**回滚
   > 任何任务质量回归 > 0.05 → 回滚
3. **将回滚改为两步**：停止将 `git checkout skill-v<N-1>` 作为盲目动作；先输出"本应回滚-因为-X"事件，要求再迭代一次确认，或要求 state 中有明确的 `allow_rollback: true` 标志。

预计工作量：3 小时。

---

## P2 — 锦上添花

### P2.1 PostToolUse hook 无法填充权威的 `subagent_log_path`

**现象**：OSR runner schema 字段 `subagent_log_path` 存在，但本应填充它的 PostToolUse hook 在编排者会话中运行，而非子 Agent 会话。Runner 自填 `""` 通过了 schema 验证，因为 `type=string` 没有 `minLength`。

**根因**：Hook 架构限制。根据 `SKILL.md:247`，Hook 是后备日志记录器——它们没有 `subagent_log.py count-by-uuid` 事后产生的 sidechain 信息。

**修复**：
1. 在 runner schema 中将 `subagent_log_path` 设为 `minLength:1`，并在 `osr_validate.py` 中添加路径必须存在于磁盘上的语义不变量（类似于 `scripts/osr_validate.py` 第 238-250 行的 skill-writer knowledge_source_refs 不变量）。
2. 在 `references/eval-loop.md` 步骤 3 中说明，编排者必须在 `subagent_log.py count-by-uuid` 返回后通过补丁 Runner OSR 来填充此字段——而非留空。将其作为 Run 阶段的步骤 3.5。

预计工作量：1 小时。

---

### P2.2 `validate_skill.py` 描述的 `MAX_DESCRIPTION_LENGTH=1024` 在必须同时编码 WHAT+WHEN+触发词时过于紧张

**现象**：四个 skill 需要多次修剪才能在保留全部三个必需元素（WHAT、WHEN、明确触发词）的同时符合 1024 字符限制。

**根因**：`scripts/validate_skill.py:56`。1024 字符是 Anthropic 规范的硬性上限，因此不可从上游修复——但当描述必须*同时*触发发现（需要触发词）和承载能力上下文（WHAT+WHEN）时，这个上限明显偏紧。

**修复**：
1. **保留上限**（这是规范要求），但添加 **Skill Writer helper** `scripts/compose_description.py`，接受 (what, when, triggers) 三部分作为独立输入，输出最佳 <=1024 字符组合，若即使最短渲染也溢出则发出警告。这样 Skill Writer 就不用手动重写了。
2. **将 WHAT+WHEN+触发词降级为 `agents/skill-writer.md:59-60` 中的提示期望**，并附上明确说明："若空间有限，优先级：触发词 > WHAT > WHEN——Claude 需要触发词来路由。"

预计工作量：1 小时。

---

### P2.3 `repo_manifest.py` 文档字符串错误地将 `--repo` 描述为可选

**现象**（Researcher meta_obs）：文档字符串第 13 行写的是 `[--repo <repo-path>]`（方括号表示可选）。实际上，如果 `workspace/repo/` 中没有目标，脚本会以"cannot find repo"（第 241 行）调用 `sys.exit(1)`。对于我们工作区与研究仓库为兄弟目录的工作流，`--repo` 实际上是必需的。

**根因**：`scripts/repo_manifest.py:228, 238-242`。发现逻辑假设 `workspace/repo/<slug>`；文档字符串未作说明。

**修复**：
1. 将使用说明重写为：`<workspace> [--repo <repo-path>] # --repo 在 workspace/repo/<anything> 存在时可省略，否则必填`。
2. 迁移到 argparse 以统一错误输出；添加 `--repo`（`required=False`）和更清晰的"在 `<workspace>/repo/` 下未找到 repo"消息，引导用户使用 `--repo`。

预计工作量：30 分钟。

---

### P2.4 `state_manager.py` CLI 参数不一致

**现象**：观察到漂移：
- `append-event` 接受 `--summary`
- `phase-transition` **不**接受 `--summary`（只有 `--to`）
- `write-iter` 接受 `--data`，而 `--content` 更符合惯用写法

**根因**：`scripts/state_manager.py:428-447`。不同作者，不同时期。

**风险**：低——错误消息清晰，没有静默失败。但长会话中认知负担累积。

**修复**：
1. 为 `phase-transition` 添加 `--summary` 并将其持久化到过渡事件中。
2. 在 `write-iter` 中将 `--content` 作为 `--data` 的别名。

预计工作量：30 分钟。

---

### P2.5 Grader worktree 的收割步骤是手动的

**现象**：Grader 在自己的 worktree 内生成 `evals/results/iter-<N>/` 产物，而非在 workspace 中。每次评分后我都要手动 `cp -R` 将其复制回 `workspace/evals/results/iter-<N>/`。

**根因**：没有脚本负责这个交接。`references/eval-loop.md:164-170` 展示了 `aggregate_grades.py --grader-dirs <g1>,<g2>,...`，它可以从 worktree 读取——但单 Grader 路径（按 SKILL.md 也是有效的）没有文档化的收割方式。

**修复**：
1. 添加 `scripts/grader_harvest.py --workspace <ws> --iter <N> --grader-dirs <list>`，将 `blind-grader-scores.json`、`grader-feedback.jsonl`、`grader-suggestions.jsonl` 从每个 Grader worktree 复制到 workspace 结果目录。K=1 时从单个目录复制；K>=2 时委托给 `aggregate_grades.py`（已处理此情况）。
2. 或者，扩展 `aggregate_grades.py` 成为无论 K 为何值的唯一收割入口点，使 K=1 成为简单的透传。在 SKILL.md §4 的 `iterate` 行中说明。

预计工作量：2 小时。

---

### P2.6 deblind 后 digest 的 `convergence.py` 子进程可以静默失败

**现象**（本次运行未观察到，但潜在存在）：`deblind_and_score.py` 第 167-172 行将 `convergence.py` 作为子进程运行，并将所有 `json.JSONDecodeError`/`ValueError` 捕获后默认返回 `converged=False, reason="continuing"`。若 convergence.py 崩溃，循环将无限运行且无任何提示。

**修复**：在非零退出时记录 `convergence.py` 的 stderr；通过 digest 中的 `anomalies` 条目暴露，而非吞噬。

预计工作量：30 分钟。

---

## 横切面观察

### X.1 系统对工具调用数有三种不同的信任层级

- **权威来源**（sidechain 日志）：适用于同步子 Agent；Super-Runner 模式不可用；异步模式不可用。
- **自报告**（`tool_use_count_self_report`）：始终可用，始终存疑。
- **统计**（`invariant_check tool_count_variance`）：跨迭代模式检测；只能检测明显的伪造（iter-N == iter-N+1 完全相同）。

**对于异步 Super-Runner 这一主要情形，三者都效果不佳。** 架构隐含地假设同步模式；SKILL.md 应正式将 Super-Runner 命名为"支持但降级的模式"，明确列出损失（轨迹信号不可用）和补偿措施（质量独占 composite，带方差检查的诚实每任务自报告）。

### X.2 "静默零值"是一种系统性反模式，在三处出现

1. `deblind_and_score.py` 在 schema 不匹配时 composite=0（P0.2）。
2. `overfit_check.py` 在 CJK 分词退化时 overlap=0（P1.2）。
3. `subagent_log.py` 在异步模式下 count=None，在 composite 公式中被用作 0（`compute_trajectory` 第 30 行将字段缺失导致的 None 视为 0）。

通用修复模式：**scorer/check 使用的任何值都应区分 `不可用` 和 `零`。** 对 `None` 的操作应传播到报告字段（`trajectory_unavailable_ratio`），而非静默地贡献零值给平均数。

### X.3 循环对自身的 `anomalies` / `surprises` 通道利用不足

本次运行中，我总共记录了约 6 个 surprises（包括我的和 Agent 提交的）。没有触发"连续 3 轮静默 → 诊断启动"守卫（`invariant_check:323-356`），因为 3 轮还不够。但 Super-Runner 轨迹问题在多轮迭代中多次以 `anomaly` 形式记录，本应累积成全循环"本次运行无法测量轨迹，回退到纯质量"决策——但没有任何机制跨迭代聚合异常重复出现并用于自动策略变更。

**修复方向**：在 `state_manager` 中添加 `recurring_anomalies` 计数器，当某个声明子字符串在 ≥2 轮迭代中重复时，将该异常升级为策略变更候选（例如"为本次运行禁用轨迹分量"）。

---

## 建议修复顺序 (Suggested fix order)

按（优先级 × 解锁其他工作 ÷ 工作量）排序：

| # | 条目 | 优先级 | 预估 | 理由 |
|---|------|--------|------|------|
| 1 | **P0.1** — worktree grader 预设 / 自动排除 `blind-mapping.json` | P0 | 1-2 h | 安全关键；实现简单；阻塞所有未来 Grader 运行。 |
| 2 | **P0.2** — deblind_and_score shape 检测 + shim | P0 | 2-3 h | 静默零值是最糟糕的失败模式；一次修复永久解决。 |
| 3 | **P0.3** — composite 公式 + 质量/轨迹拆分 | P0 | 3-4 h | 不修复此项，每次迭代都可能误触发回滚。 |
| 4 | **P1.2** — CJK 感知的 overfit_check tokenizer | P1 | 2 h | 成本低；立即改善所有中文 Skill Writer 的信噪比。 |
| 5 | **P1.3** — osr_validate 自动截断 | P1 | 2 h | 减少拒绝循环混乱，加快每个研究阶段。 |
| 6 | **P1.6** — 回滚策略拆分（与 P0.3 配对） | P1 | 3 h | 与 P0.3 同时处理；策略变更应与公式变更同步。 |
| 7 | **P1.1** — Super-Runner 模式正式化 + schema 收紧 | P1 | 1 天 | P1 中架构变更最大；解锁诚实的轨迹故事。 |
| 8 | **P1.4** — 饱和 → 评测扩展流程 | P1 | 4-5 h | 价值随运行长度增加；若运行止于 iter-2 则不紧迫。 |
| 9 | **P2.5** — grader_harvest.py | P2 | 2 h | 消除操作摩擦；提高可复现性。 |
| 10 | **P1.5** — 两阶段 Eval Designer | P1 | 1 天 | 最大设计变更；等 1-9 项提供干净信号后再推进。 |
| 11 | **P2.1 / P2.2 / P2.3 / P2.4 / P2.6** | P2 | 约 3 h 合计 | 生活质量；合并成一个清理提交。 |

**机会性分组**：P0.3 + P1.6 必须一起发布（公式 + 策略）。P0.1 + P2.5 都与 worktree 相关，可以共享一个 diff。

**不要单独发布**：P0.3 不含 P1.6，或反之——前者会导致每个质量正向迭代都被自动回滚（若保留策略但修复公式返回 None），后者会彻底失去回滚安全网（若软化策略但不修复公式）。

---

## 附录：本次运行的原始观察

### A. 最终评分表（iter-2 vs iter-1）

- 12/12 任务：`skillWon=yes`
- 平均质量 4.58/5（iter-1 为 4.33）
- composite 0.55（iter-1 为 0.6438；delta -0.09）
- **每个任务的 `composite_delta <= -0.189`**——尽管质量全面提升，却全部判为"回归"。这正是 P0.3 中描述的轨迹信号崩溃的教科书案例。

### B. 出现问题的文件/位置（便于 grep）

- `scripts/worktree_helper.py:55-67, 89, 124-130`（P0.1）
- `scripts/deblind_and_score.py:28-31, 64-65, 72-93`（P0.2, P0.3）
- `scripts/subagent_log.py:148-159`（P1.1）
- `scripts/overfit_check.py:37, 40-42`（P1.2）
- `scripts/osr_validate.py:143-148`（P1.3）
- `scripts/invariant_check.py:263-292`（P1.4）
- `scripts/validate_skill.py:56`（P2.2）
- `scripts/repo_manifest.py:228, 238-242`（P2.3）
- `scripts/state_manager.py:428-447`（P2.4）
- `schemas/osr-researcher.schema.json:39,40,60,96`（P1.3）
- `SKILL.md:111, 224` 和 `references/eval-loop.md:115,126`（P0.1, P1.6）

### C. 运作良好的方面（平衡视角）

- **OSR 协议结构**表现良好——一旦绕过 schema 问题，它承载了编排者所需的确切信息。`surprises`/`anomalies`/`meta_observations` 通道是承重结构，三者我都用到了。
- **`osr_validate` + `overfit_check` 作为后备**捕获了一个真正的走形式引用（隐藏在 CJK 误报噪声中）——因此这个检查并非纯噪声。
- **通过 worktree 实现的物理隔离**是正确的架构；bug 在于边界（什么被复制进/出），而非概念本身。
- **State manager 的 Markov 恢复特性**在我暂停后继续运行时得到了隐式测试——运作正常。
- **集成设计（即使我们运行了 K=1）**意味着当轨迹噪声占主导时路径是可用的；`aggregate_grades.py` + `ensemble-metrics.json` 已就绪，可随时部署。
