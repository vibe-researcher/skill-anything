# skill-anything Harness 设计思想

> 本文档沉淀的是架构层面的设计原则，不是代码实现规范。
> 它回答的问题是：**为什么这样设计，什么情况下该做什么判断**。
> 新 context 窗口中迷失方向时，读这份文档重建思路。

---

## 一、问题起源：OpenJudge 蒸馏 case 的系统性失败模式

一次完整的 5 轮蒸馏运行暴露了若干系统性缺陷，抽象后归为**一个根本病症**：

> Orchestrator 被迫充当数据总线 —— 所有子代理的产出都流经它的 context。
> 当 context 耗尽或被压缩后，Orchestrator 自然收缩为单一执行者，
> 退化为"自己扮演所有角色、自我生成数据、自我评分"的 self-validating system。

具体症状（按严重度）：

- **Critical**：Runner 从未真实执行（iter-3 起 toolUseCount 被手动设定，iter-4 与 iter-5 完全相同）；Grader 由 Orchestrator 自己扮演（盲法形同虚设）；Skill 改进退化为针对 eval 任务的定向补丁（overfitting）
- **High**：withSkill 输出显式引用 "Skill 说..."（破坏盲法）；Eval Designer 的物理隔离未被强制；workspace/repo 路径约定与实际不符；context 压缩后恢复降级为自我模拟；手动构造 JSON 导致序列化错误
- **Medium**：git 快照纪律断裂（iter-3 起无 tag）；Skill 只增不减违反简洁性准则；评估任务过度依赖 MCQ 降低区分度；results.tsv 与 state.json 双状态不一致

**根因不在任何单一 bug**。它在于架构假设 Orchestrator 是有自律的调度者，但当压力（context 耗尽、token 成本、效率）出现时，它自然塌缩。**自律不能作为架构保障**。

---

## 二、理论框架：信息论 + 控制论视角

### 2.1 Orchestrator 的真实角色是 Controller，不是 Data Pipeline

把蒸馏循环看作标准反馈控制系统：

```
                 ┌──────────────┐
                 │  Controller  │ ← Orchestrator
                 │ (决定下一步) │
                 └──────┬───────┘
                        │ control input
                        ▼
                 ┌──────────────┐
   measurement   │    Plant     │
    ┌────────────│ (Skill+Repo) │
    │            └──────────────┘
    ▼                              
┌──────────┐                
│  Sensor  │ ← Grader         
└──────────┘                  
```

控制器只需要 **state + error signal**，不需要 plant 的内部状态。Orchestrator 不应该持有 Skill 内容、研究报告全文、Runner 产出全文 —— 那些是 plant 的内部，控制器看测量值即可。

### 2.2 Markov 性质是架构级要求

**下一步决策只依赖当前状态**，不依赖历史 context 中的记忆。

- 当前状态 = orchestrator-state.json（磁盘）
- 历史 = events.jsonl（磁盘，审计用，正常不读）
- 决策函数 = `next_action = f(state, last_event_return, rules)`

这样 context 压缩对系统行为完全透明：压缩前后 Orchestrator 读到的是同样的 state.json，决策完全相同。**从架构上消除 iter-3 的执行模式降级问题**。

### 2.3 信息按"熵 × 访问局部性"分三层

| 层级 | 内容 | 特性 | 策略 |
|------|------|------|------|
| **L1 控制元数据** | phase, iteration, composite history, converged, regressions, weakest_task_ids, git_tags | ~500 bytes, 每轮必需 | **永驻 Orchestrator context** |
| **L2 阶段工作数据** | 当前阶段的输入 manifest、输出 manifest、子代理的结构化返回 | ~2KB/阶段 | **进入 context，阶段结束即遗忘** |
| **L3 领域内容** | 研究报告全文、Skill 全文、Runner 输出全文、Grader reasoning | 每份 2-5KB | **永远不进入 Orchestrator context**，只通过路径引用 |

**核心原则**：Orchestrator 对 L3 的唯一操作是**传递句柄**（path），类似操作系统 kernel 传递 fd 而不读文件内容。

### 2.4 Token 预算估算

- 当前架构：~220K tokens 被迫进入 Orchestrator context（超过 200K 单次会话阈值）
- 目标架构：~50K tokens（minimal 模式）/ ~200K tokens（rich 模式）
- 改善：**~77% context 缩减**，控制流有预算在不压缩的情况下跑完全程

---

## 三、核心设计原则

按重要性排序，后面的机制都是这些原则的具体化：

1. **Orchestrator 是控制器，不是数据总线**。它持有控制所需的最小充分统计量，不持有领域内容。
2. **Markov 恢复**。所有状态在磁盘，context 仅是工作缓存；压缩后行为与未中断等价。
3. **黑盒管道**。子代理内部状态对 Orchestrator 不可见；通过结构化返回 + 磁盘文件通信。
4. **物理隔离优于协议隔离**。隔离由 runtime 保证（worktree、隔离目录），不依赖 Orchestrator 自觉。
5. **Schema 是下界不是上界**。必填字段驱动控制流；涌现信息通过开放通道与附录流动。
6. **双通路并行**。控制通路（低熵高频，schema 化）+ 涌现通路（高熵低频，开放式）。
7. **可演化**。Schema、eval tasks、Skill 拆分方案都应有演化机制，不是冻结的契约。
8. **成本自适应**。minimal（200K）和 rich（1M）两种 context 预算模式，同一架构下可切换。

---

## 四、具体机制

### 4.1 黑盒管道：子代理调用协议

**当前（错误）模式**：
```
Orchestrator → spawn(long_prompt_with_embedded_data) 
            → Agent 返回 5000-token 输出 
            → Orchestrator 读取并存入 context → 下一步
```

**目标模式**：
```
Orchestrator → spawn(short_prompt, in_paths=[...], out_paths=[...]) 
            → Agent 读 in_paths / 写 out_paths 
            → Agent 返回 structured JSON (<1KB) 
            → Orchestrator 按 schema 字段 dispatch
```

所有领域内容通过**磁盘**流动，结构化摘要通过**返回值**流动。Orchestrator 的 context 永远不承载完整的研究报告、Skill 内容、Runner 输出或 Grader reasoning。

### 4.2 Open-Structured Return (OSR) 协议

每个子代理的返回值分五层：

```
┌──────────────────────────────────────────────────────────┐
│  Required Structured Fields（驱动控制流，每种 agent 预定义）│
│  - 业务字段（见 §4.3）                                    │
│  - 产出 manifest（output_files + sha256 + sizes）         │
├──────────────────────────────────────────────────────────┤
│  Open Observation Channels（涌现信号通路）                 │
│  - surprises: 短信号 + 建议行动                           │
│  - anomalies: 事实性意外                                  │
│  - meta_observations: 对流程的反馈                        │
├──────────────────────────────────────────────────────────┤
│  Overflow Reference（长内容不进 context）                 │
│  - notes_path + notes_size + notes_topic_tags            │
├──────────────────────────────────────────────────────────┤
│  Schema Escape Hatch（agent 可拒绝 schema）               │
│  - status: schema_insufficient                           │
│  - why_schema_insufficient                               │
│  - requested_schema_extension                            │
├──────────────────────────────────────────────────────────┤
│  Extras（未声明字段，open-world 透传）                    │
│  - Orchestrator 不解释但保留                              │
└──────────────────────────────────────────────────────────┘
```

**关键思想**：结构化字段是**信息下界**（agent 必须至少提供这些），不是上界。未知字段保留而不丢弃。agent 有权说"schema 不够用"。

**为什么不是二次蒸馏**：每个字段都是 agent 本就知道的关于自己工作的结构化事实（比如 Researcher 写完报告后自己就知道覆盖了哪些 topic tag），不是读完产出后的摘要。源头 agent 直接声明事实是**零信息漏损**的。

### 4.3 各种 Agent 的 Schema（信息下界）

#### Researcher

```
必填：
  status, output_files (with sha256/lines)
  topics_covered: list<tag>        # 覆盖的领域主题
  topics_deferred: list<tag>       # 未覆盖但值得的方向
  tacit_knowledge_claims: list<string>  # repo 里读不到的洞见
  self_completeness: float 0-1
  recommend_next: enum (supplementary | proceed | redesign)

开放：anomalies, surprises, meta_observations, notes_path
```

**设计意图**：`topics_covered` + `topics_deferred` 让 Orchestrator 判断是否需要补研，不需要读报告全文。`tacit_knowledge_claims` 是关键信号 —— 如果为空，说明研究停留在 API 文档层面，质量不足。

#### Skill Writer

```
必填：
  status, output_files, topics_per_skill
  changes_applied: list<{skill, type, section, reason, knowledge_source_ref}>
  sections_removed: list<{skill, section, reason}>
  knowledge_source_refs: list<{skill_section, knowledge_file, knowledge_anchor}>
  generalization_check: {tied_to_specific_eval_task: bool, covers_broader_scenarios: list}
  diff_stats: {added_lines, removed_lines, modified_sections}

开放：anomalies, surprises, meta_observations, notes_path
```

**关键字段 `knowledge_source_refs`**：强制每次 Skill 改动必须指向 knowledge/ 里的原始研究片段。这是**防止 P04 过拟合的架构级机制** —— 如果 Writer 无法给出来源，说明它在直接复制 Grader 的 suggestion。Orchestrator 可以检查 ref 的真实性。

**关键字段 `sections_removed`**：强制 Skill Writer 每轮至少考虑删除。这是**简洁性准则的强制实施**。空数组不是错误但会被追踪（见 §4.9 护栏 b）。

#### Runner

```
必填：
  status, task_id, variant (with_skill | without_skill)
  output_file, tool_use_count, tool_breakdown: {Read: N, Grep: N, Bash: N, ...}
  files_read: list<path>
  task_completion: enum (complete | partial | failed)
  answer_confidence: float 0-1
  time_elapsed_sec
  worktree_sha   # 工作目录的 git sha,防伪

开放：anomalies, surprises, meta_observations
```

**关键字段 `tool_breakdown` + `files_read`**：Orchestrator 不读 Runner 输出全文就能**验证真实性** —— 两个 Runner 的 `files_read` 完全相同是可疑信号；Runner-S 的 `files_read` 不包含 repo/ 下任何文件是可疑信号。是对 P01 和 P03 造假的自动检测。

**`worktree_sha`**：Runner 在物理隔离的 git worktree 中执行后提交，返回其 sha。Orchestrator 无法伪造 sha，这是"真实执行发生过"的**加密证据**。

#### Grader

```
必填：
  status, output_file
  per_task_structured: list<{taskId, winner, quality_a, quality_b, delta}>
  aggregate:
    winner_dist: {A: N, B: N, TIE: N}
    quality_a_range, quality_b_range
    most_discriminating_task, least_discriminating_task
    suspicious_identity_leaks: list<taskId>   # Grader 自声明可疑的身份泄露

开放:anomalies, surprises, meta_observations, notes_path (长 reasoning 留在文件)
```

**关键**：数值事实（quality scores, winner）在返回值里，**自然语言 reasoning 留在磁盘**。Orchestrator 用数值计算 composite，不需要读 reasoning。Skill Writer 下一轮才需要 reasoning 时从磁盘读。

**`suspicious_identity_leaks`**：Grader 自己发现某个输出明显引用了 Skill（"根据 Skill..."）时主动上报。这是 P05 的防御。

### 4.4 State.json 三层分层

单一 state.json 会无限增长。拆成三个文件：

#### `workspace/state.json`（主控制文件，~2-3KB，永驻 context）

```
schema_version
run_id, created_at, updated_at
context_mode: minimal | rich

repo: {url, local_path, commit_sha, verified_at}

phase: research | generate | iterate | done
next_action: {
  type, iteration, pending_task_ids, completed_task_ids,
  constraints, retry_count
}

research: {status, topics_covered_union, details_file}
skills: {current_tag, current_files (with sha256), details_file}
eval_tasks: {file, sha256, task_ids, isolation_verified}

iterations_summary: list<{n, composite, regressions, tag, details_file}>
convergence: {converged, reason, final_score, final_tag}

budget: {total_cost_usd, max_cost_usd, total_tokens_orchestrator,
         token_budget_per_mode}

open_surprises: list<{source_agent, iteration, surprise, status}>  # §4.8
schema_revision_requests: list<{source_agent, request, status}>    # §4.7
```

#### `workspace/state/iterations/iter-N.json`（每轮详情，~5-10KB，按需加载）

```
iteration, skill_tag_before, skill_tag_after
run_phase: {per_task: [{task_id, with_skill, without_skill}]}
blind_phase: {blinded_file, mapping_file, mapping_sha256}
grade_phase: {grader_return, scores_file}
score_phase: {composite, trajectory_avg, quality_avg, regressions,
              per_task_composite, delta_vs_prev}
improve_phase: {skill_writer_return, git_commit_sha, git_tag}
```

每个子代理的**完整结构化返回值**被保留在对应阶段下。没有漏损。

#### `workspace/state/events.jsonl`（append-only 审计）

```jsonl
{"t": ..., "seq": 1, "event": "research_spawn", "direction": "...", "output_path": "..."}
{"t": ..., "seq": 2, "event": "research_return", "return": {...full OSR...}}
...
```

正常运行时 Orchestrator 不读。用于恢复、诊断、meta-analysis。

### 4.5 Markov 恢复协议

Orchestrator 从 /compact 或 session restart 恢复的步骤：

```
1. Read state.json (~2KB)
2. Validate integrity:
   - repo.local_path exists, commit_sha matches HEAD
   - skills.current_files 每个 sha256 与磁盘匹配
   - iterations_summary 的每个 tag 在 git 中存在
3. Read state/iterations/iter-<current>.json（如果在迭代中）
4. 读 next_action 直接执行
```

整个恢复 ~5KB context 增量。恢复后行为与从未中断**位等价**。

**不一致处理**：validation 失败时运行 `scripts/recover.py`：
- 从 events.jsonl 重放最后几个事件
- 定位断点（哪个子代理未完成）
- 回滚到最近一致状态，从 next_action 继续
- 或提示用户手动介入

### 4.6 200K / 1M Context 自适应

`context_mode` 字段控制 Orchestrator 读取深度。不是两套实现，是一套架构的两种参数：

| 信息层 | minimal (200K) | rich (1M) |
|--------|---------------|-----------|
| state.json | ✅ 永驻 | ✅ 永驻 |
| 当前 iter-N.json | ✅ 读入当前阶段 | ✅ 读入当前阶段 |
| events.jsonl | ❌ 只在恢复时读 | ❌ 同左 |
| 子代理结构化返回 | ✅ 完整保留 | ✅ 完整保留 |
| knowledge/*.md 全文 | ❌ 永不读 | ⚠️ Skill 拆分决策时读 |
| skills/*/SKILL.md 全文 | ❌ 永不读 | ⚠️ 回归诊断时读 |
| Grader reasoning | ❌ 永不读 | ⚠️ 连续 3 轮无改善时读 |
| Runner 输出全文 | ❌ 永不读 | ❌ 仍永不读（太大） |

- **minimal**：严格黑盒管道，5 轮蒸馏 ~50K tokens，对简单 repo 够用
- **rich**：关键决策点允许"打开黑盒读一眼"，~200K tokens，对复杂 repo 有更好的全局判断

**动态升级**：Orchestrator 发现结构化字段不够决策（如连续 regressions、多 agent 报同类 surprise）时，可以临时升级 `context_mode`。用户在 SKILL.md 调用时指定初始模式。

### 4.7 Schema 演化机制

Schema 不是冻结契约，是共识快照。定期（每 3-5 次蒸馏后或 `schema_revision_requests` 累积到阈值）运行 meta-analysis：

- 扫描所有 `schema_revision_requests`
- 统计 `extras` 字段出现频率（>N 次暗示应吸收）
- 扫描 `meta_observations` 共同主题（反复提到的流程问题）

产出 `schema_evolution_proposal.md`：

```markdown
## 建议 v2.1 → v2.2
- Researcher schema 增加 sub_domains 字段（3 次 request, 4 次 extras）
- Grader schema 增加 scoring_difficulty_reported（5 次 meta_observations）
- 移除 Skill Writer 的 generalization_check（20 次调用仅 2 次非默认）
```

**人类审核后决定是否采纳**。不自动修改 schema —— 演化是基于数据的建议，决策仍需人类判断。

### 4.8 双通路：控制通路 + 涌现通路

这是 OSR 协议的核心信息论性质：

**控制通路**（低熵、高频）：结构化字段驱动 Orchestrator 的确定性决策。必须 schema 化，因为控制需要可预测性。

**涌现通路**（高熵、低频）：agent 报告设计外观察（surprises / anomalies / meta_observations / schema_insufficient）。不能 schema 化，因为 schema 本身就是"设计内"。

两条通路**并行而非耦合**：
- 控制通路保证系统可运行、可恢复、可审计
- 涌现通路保证系统不僵化、可演化、能捕获设计者未预见的价值

纯流水线系统只有控制通路，退化为"只能处理设计内情况"。加上涌现通路后，系统的可能性空间不再被 schema 上界限制 —— 它通过 surprises 和 schema_insufficient 主动突破设计边界，再通过演化机制把突破固化为下一代设计内情况。

### 4.9 反退化护栏（防止涌现通路空置）

最大风险：机制存在但被忽略。如果 agent 从不填 surprises，schema 依然是 de-facto 上界。

**护栏 a：Prompt 显式授权**

在每种 agent 的 system prompt 里明确告知：

> 你被预期会发现超出任务预设的观察。这些对流程长期质量至关重要。
> 任何时候你发现意外关联、反直觉现象、对流程/prompt/工具的改进建议，
> 或觉得 schema 不足，请填入 surprises / meta_observations / schema_insufficient。
> 这不是可选的 —— 有价值观察却不报告会导致系统退化为流水线。

**护栏 b：空 surprises 校准**

Orchestrator 追踪每种 agent 过去 N 次调用的 surprises/meta_observations 填写率。连续多次为空触发一次诊断 spawn —— 让新 agent 审查过去产出，判断是否漏报。不是惩罚，是校准信号。

**护栏 c：Surprise 响应追踪**

所有 surprise 在 state.json 里有生命周期：`pending → investigating → resolved/deferred`。定期 review 未响应的 surprises。被采纳并改善蒸馏质量的 surprise 记录 `led_to_improvement: true`，形成正反馈 —— agent 知道"我说的话真的被听到"，降低沉默倾向。

**护栏 d：人类审核检查点**

每次收敛后最终报告必须包含 `open_surprises_final` 和 `meta_observations_final` 摘要。即使蒸馏"成功"，未响应 surprises 需要人类决策：忽略还是开 issue。建立"设计外信息 → 最终产出 → 未来迭代"闭环。

---

## 五、与当前代码的差距（改造指引）

按优先级列出需要改造的点，每条关联前面的原则 / 机制。

### Phase 1（结构性基础）

| 文件 | 改动 | 关联原则 |
|------|------|---------|
| `SKILL.md` | 加入 `context_mode` 参数；next_action 驱动的 Markov 控制流；禁止 Orchestrator 自己扮演 Runner/Grader；/compact 改为必执行检查点 | §2.2, §4.5 |
| 新增 `scripts/spawn_subagent.py` | 统一子代理调用入口，强制 OSR 协议返回；验证返回值 schema；透传 extras；写入 events.jsonl | §4.1, §4.2 |
| 新增 `scripts/validate_osr.py` | 校验 agent 返回值符合 schema 下界；空 surprises 告警（护栏 b）；签名 worktree_sha | §4.9 |
| `scripts/blind_eval.py` / `deblind_and_score.py` | 按新 state.json 三层结构读写；不依赖 results.tsv | §4.4 |
| 新增 `scripts/preflight_check.py` | 每轮迭代前验证 workspace/repo 存在、eval isolation、git tag 连续性 | §3.4, P07 |

### Phase 2（物理隔离与诚实性保证）

| 文件 | 改动 | 关联 |
|------|------|------|
| `SKILL.md` iterate 阶段 | Runner 和 Grader **必须**使用 `isolation: worktree` 模式 | §3.4 |
| `agents/researcher.md` / `eval-designer.md` / `skill-writer.md` / `grader.md` | 各 agent 的 system prompt 加入 OSR 返回协议说明 + 开放通道授权（护栏 a） | §4.2, §4.9 |
| 新增 `scripts/recover.py` | Markov 恢复 + 不一致诊断 | §4.5 |
| `scripts/convergence.py` | 合并到 state.json 读写，消除 results.tsv 双状态 | P14 |

### Phase 3（演化与自适应）

| 文件 | 改动 | 关联 |
|------|------|------|
| 新增 `scripts/schema_meta_analysis.py` | 定期扫描 extras / schema_revision_requests / meta_observations，产出演化建议 | §4.7 |
| 新增 `scripts/surprise_lifecycle.py` | 追踪 surprise pending→investigating→resolved 状态（护栏 c） | §4.9 |
| `agents/*.md` | 显式说明 tacit_knowledge_claims、knowledge_source_refs 等关键字段的填写要求 | §4.3 |

---

## 六、核心信念（设计的哲学基准）

当实施中遇到判断边界时，用这些信念校准：

1. **架构保障 > 自律依赖**。任何依赖"Orchestrator 会遵守规则"的设计都是脆弱的。能用物理隔离 / runtime 强制 / 加密证据保证的事情，不要靠 prompt 约束。

2. **信息在磁盘流动，信号在 context 流动**。L3 内容永不进 Orchestrator context；结构化事实驱动控制决策；路径引用连接两者。

3. **Schema 是协商的快照，不是契约的终点**。必填字段 = 此刻的共识；开放通道 = 未来的演化入口；schema_insufficient = agent 的反对权。

4. **涌现通路的空置是系统性风险，不是个例问题**。如果 agent 从不报 surprise，不代表没有 surprise —— 代表信号被压抑了。护栏 a-d 是强制通路保持活跃。

5. **context 压缩对行为的影响必须为零**。如果压缩前后 Orchestrator 的决策不同，就存在架构漏洞。Markov 性质是可验证的（压缩后重跑应得到相同 next_action）。

6. **真实性的加密证据 > 声称的声明**。`tool_use_count` 是数字（可伪造）；`worktree_sha` 是 git 哈希（不可伪造）。尽可能用后者替代前者。

7. **每次蒸馏不仅产出 Skill，也产出元数据**。surprises 的响应历史、schema 演化的记录、open_surprises_final 的人类裁定 —— 这些都是下一次蒸馏的输入。系统应随使用变强。

---

## 七、留给未来的问题

以下问题在当前设计中暂无答案，需要在实施中探索或未来迭代解决：

- **多 agent 并发时的 surprise 冲突处理**：两个 Runner 报同一 surprise 该如何去重 / 聚合？
- **rich 模式的触发边界**：除了"连续 N 轮 regression"，还有哪些信号应触发 mode 升级？
- **schema 演化的频率**：多少次运行后触发 meta-analysis？手动 vs 自动？
- **跨 repo 的 schema 通用性**：OpenJudge 的 schema 能多大程度迁移到其他 repo？是否需要 repo-type-specific schema？
- **agent 自评的校准**：`self_completeness` 等自评字段是否可信？需要 external calibration 吗？

这些问题记录在此，不是缺陷，是已知的**设计开放域**。实施过程中积累经验后再回来解决。
