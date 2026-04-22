# 迭代循环（Eval Loop） — v2

每轮按 **Run → Blind → Grade → Score → Improve** 顺序执行。v2 相对于 v1 的核心变化：

- **物理隔离替代协议隔离**：所有 Runner/Grader/Eval Designer 在由 `worktree_helper.py` 创建的独立目录中运行。Orchestrator 物理上无法自己扮演这些角色——它无权查看 `workspace/.worktrees/` 下的工作目录。
- **OSR 结构化返回**：每个子代理返回结构化 JSON（schema 在 `schemas/osr-*.schema.json`），Orchestrator 机械消费字段而非解析自然语言。
- **toolUseCount 来自 subagent_log.py**：Orchestrator 不信 Runner 自报；以 `subagent_log.py` 从 Claude Code session 日志抽取的值为准。

---

## Run：运行对比评估

对每个 eval task，按 **with_skill / without_skill 两个变体**分别 spawn Runner 子代理。两个 Runner 都拥有 `repo/` 副本；**with_skill** 额外拥有 `skills/`。

### 步骤 1：创建物理隔离的工作目录

```bash
# Runner+S（有 Skill 访问）
python3 scripts/worktree_helper.py create \
    --workspace workspace/ \
    --purpose runner-with-iter<N>-<task_id> \
    --include repo,skills
# → 返回 {work_path, manifest_path}

# Runner-S（无 Skill 访问，强制 forbid）
python3 scripts/worktree_helper.py create \
    --workspace workspace/ \
    --purpose runner-without-iter<N>-<task_id> \
    --include repo --exclude-guard skills
# → worktree_helper 会在创建后 verify skills/ 不存在，不存在才返回；
#    若以任何方式泄漏（例如符号链接），创建会失败。
```

两个 Runner 的 `work_path` 都包含 `.isolation.json` 清单文件，记录了包含哪些 include 和哪些 exclude_guards。Orchestrator 把 `work_path` 传给子代理作为 CWD。

### 步骤 2：Spawn Runner 子代理

```
任务提示（传给 Task tool，subagent_type=runner）：

你是一个独立的开发者 Agent。
工作目录：<work_path>（这是一个物理隔离的副本）
任务描述：<eval task 的 description>

你可以使用以下子目录的任何文件：
- repo/     ← 项目源码
{如果 with_skill}
- skills/   ← 蒸馏的 Skill 文件。任务开始前请先阅读 skills/**/SKILL.md

⚠ 严格约束：
1. 不得离开工作目录（不要访问上级目录的任何文件）
2. 不得在输出文本中提及 "Skill"、"SKILL.md" 或引用 skills/ 路径
3. 回答完成后，输出严格 JSON 遵循 schemas/osr-runner.schema.json，写到 work_path/osr.json

必填字段参考 osr-runner.schema.json；如果 schema 无法表达你的发现，
使用 status=schema_insufficient 并声明 requested_schema_extension。
```

### 步骤 3：收集 OSR 并抽取真实 toolUseCount

每个 Runner 完成后，Orchestrator 执行：

```bash
# 1. 校验 OSR 结构
python3 scripts/osr_validate.py --agent runner --input <work_path>/osr.json

# 2. 抽取真实 toolUseCount（权威来源）
LOG=$(python3 scripts/subagent_log.py find-latest-log \
    --project-dir ~/.claude/projects/-Users-chr-Dev-skill-anything \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['log'])")
python3 scripts/subagent_log.py count-by-uuid \
    --log "$LOG" --tool-use-id <Agent tool_use id>
```

`count-by-uuid` 的输出有三种 source：
- `sidechain_log` → 权威计数（从 Claude Code 会话日志抽取）
- `not_available` + `reason=async_not_in_log` → 异步子代理未在日志中记录；此时以 Runner 自报（`tool_use_count_self_report`）为准，**并标记为 anomaly-eligible** — Orchestrator 在 OSR 的 `anomalies` 字段里追加一条 `{claim: "tool_count unverified (async)", evidence_path: <subagent_log result path>}`

### 步骤 4：记录到 iter-N.json

```bash
# state/iterations/iter-N.json 汇总所有 task 的两个变体
python3 scripts/state_manager.py workspace/ write-iter --iter N \
    --data-from-file --data evals/results/iter-N/iteration-draft.json
```

---

## Blind：盲化（脚本自动完成，未变更）

```bash
python3 scripts/blind_eval.py workspace/ <N>
# → evals/results/iter-N/blinded-eval-results.json
# → evals/results/iter-N/blind-mapping.json
```

`blinded-eval-results.json` 包含 outputA / outputB 对（随机化）；`blind-mapping.json` 是 ground truth（哪个 output 来自 withSkill）。**后者只在 Score 阶段由 deblind_and_score.py 读取，Grader 绝对无权访问**。

---

## Grade：盲法评判（ensemble × 物理隔离）

Grade 阶段以 **K 个独立 Grader 的 ensemble** 执行（默认 K=3；K=1 自动降级为旧单 grader 行为）。目的是让"评判分歧"从 invisible 变成 evidence-level 信号：单 grader 偏好稳定但偏移不可测；ensemble 可量化一致性、暴露 eval 本身的 ambiguity。

### 步骤 1：为每个 Grader 创建隔离环境

```bash
for i in 1 2 3; do
  python3 scripts/worktree_helper.py create \
      --workspace workspace/ \
      --purpose grader-iter<N>-g${i} \
      --include "evals/results/iter-<N>" \
      --exclude-guard "skills,state"
done
# K 个 worktree 互不共享；exclude-guard 保证 skills/ 与 state/（含 blind-mapping.json）不被带入任何一个
```

显式只拷贝 `blinded-eval-results.json` 和 `eval-tasks.json` 到**每个** work_path：

```bash
for g in <g1_work_path> <g2_work_path> <g3_work_path>; do
  cp workspace/evals/results/iter-<N>/blinded-eval-results.json "$g"/
  cp workspace/evals/eval-tasks.json "$g"/
done
# 绝不拷贝 blind-mapping.json
```

### 步骤 2：权威 toolUseCount 注入（所有 K 份输入）

Grader 的 `per_task[].tool_count_a_from_log` 和 `tool_count_b_from_log` **必须来自 `subagent_log.py`**，不是 Runner 自报。Orchestrator 把解析后的值**相同地**注入所有 K 个 Grader 的输入——聚合脚本会 assert 一致性，任何分歧视作 anomaly。

### 步骤 3：并行 Spawn K 个 Grader

同一 prompt 模板、K 个独立 Task，并行发出：

```
任务提示（subagent_type=grader，共 K 份）：

你是 Grader（ensemble member #<i> of <K>，但评判时**忽略此编号**——它只用于
输出文件区分，不得影响你的判断；也不代表你在与其他 grader 协同，你是独立评判）。

工作目录：<grader_work_path_i>
任务：对 blinded-eval-results.json 中每个 task 的 outputA/outputB 做盲法质量评判。
你不知道也不应推测哪个 output 用了 Skill。

可用文件：
- blinded-eval-results.json   ← 被评判的数据（已注入 tool_count_*_from_log）
- eval-tasks.json             ← 任务定义

严格约束：
1. 不得访问 blind-mapping.json（不在工作目录中）
2. 如果某个 output 明显引用了 "Skill"/"SKILL.md"，
   blind_discipline_check.referenced_skill_files=true，但仍基于输出质量评判
3. 输出 JSON 遵循 schemas/osr-grader.schema.json → work_path/osr.json
   完整 reasoning/feedback/suggestion 文本同步写到 blind-grader-scores.json
4. 【边界清晰性】若某 task 你感到量表不足或判据模糊，用 winner="TIE" 诚实标注，
   不要为维持区分度而强分胜负——这比假自信对 ensemble 更有价值
```

### 步骤 4：聚合

所有 K 个 Grader 返回后：

```bash
python3 scripts/aggregate_grades.py \
    --workspace workspace/ --iter <N> \
    --grader-dirs <g1_work>,<g2_work>,<g3_work>
# → evals/results/iter-<N>/blind-grader-scores.json   (聚合后；下游 deblind_and_score.py 不变)
# → evals/results/iter-<N>/ensemble-metrics.json      (一致性指标)
```

聚合规则：
- `winner`：多数投票；K=2 分裂时看 |median(quality_a) - median(quality_b)| > 0.5 破平
- `quality_a/b`：中位数（抗单 grader outlier）
- `reasoning/feedback/suggestion`：按 `[g1] ... [g2] ...` 标签串接（Skill Writer 下游能看到多视角）
- `tool_count_*`：所有 K 份应相同，否则记为 anomaly

**ensemble-metrics.json** 的关键字段：
- `mean_winner_agreement` ∈ [0,1]：越低说明评判分歧越大
- `disagreement_tasks`：winner 分裂 / quality stdev > 1.0 / tool_count 不一致的 task
- 这些会被 `invariant_check.py --check grader_ensemble_agreement` 消费，自动写入 anomalies

### 步骤 5：OSR 校验（每个 Grader 独立）

```bash
for g in <g1_work> <g2_work> <g3_work>; do
  python3 scripts/osr_validate.py --agent grader --input "$g"/osr.json
done
# 任一失败 → 修正 prompt 重 spawn 该 Grader（不影响其他 K-1 个）
```

---

## Score：反盲化 + 评分 + 收敛（脚本自动完成）

```bash
python3 scripts/deblind_and_score.py workspace/ <N> --cost <USD>
```

脚本自动完成：
1. 读取 `blind-mapping.json` 反盲化
2. 用 with-Skill 侧的评分计算 composite
3. 检测回归（与上一轮对比）
4. 调用 `convergence.py` 判断收敛
5. 产出：
   - `grader-scores.json`（完整格式，人类可读）
   - `osr-grader-digest.json`（schema-validated 子集，Orchestrator 机械消费）
   - `iteration-summary.json`（紧凑摘要）
6. append 事件到 `workspace/state/events.jsonl`

### 评分公式

```
composite = (quality/5) * 0.6 + trajectory * 0.4
trajectory = max(0, 1 - toolCallsWith / toolCallsWithout)
```

**重要**：`toolCallsWith` / `toolCallsWithout` 只能来自 Grader OSR 的 `tool_count_*_from_log` 字段。若某条记录的 from_log=null（异步未捕获），则 trajectory 组件为 0，并在 guardrail_flags 追加 `{name: trajectory_inference_blocked, severity: warn}`。

### stdout 输出（Orchestrator 唯一需要读的）

```
composite=0.72 converged=false reason=continuing
weakest: task-2, task-5
details: workspace/evals/results/iter-1/iteration-summary.json
osr_digest: workspace/evals/results/iter-1/osr-grader-digest.json
```

---

## Improve：改进 Skill（含反过拟合约束）

### 步骤 1：分析信号

读取：
- `iteration-summary.json`（composite、regressions、weakest_tasks、feedback/suggestion 摘要，每条 ≤200 字符）
- **必要时**读 `osr-grader-digest.json`（在 rich context_mode 下）

### 步骤 2：Spawn Skill Writer（带约束）

```
任务提示（subagent_type=skill_writer）：

改进 Skills 以应对本轮弱项。输入：
1. knowledge/*.md        ← 研究报告（你的**唯一合法知识源**）
2. skills/*/SKILL.md     ← 当前 Skill（上轮版本）
3. state/iterations/iter-<N>.json 的 iteration_summary 部分
   （不是 feedback 全文；长文本在 <digest_path>）

【反过拟合硬约束】
- 每一条 changes_applied 的 knowledge_source_refs 必须指向 knowledge/*.md 的具体行号；
  空 refs 会被 overfit_check.py 拒绝
- rationale_short 中不得出现 eval task id 字面量（如 'T5', 'mixed-scale-aggregation'）
  — 这会被 overfit_check 标红

【简洁性准则】
- 每次改进请同时考虑**减法**。若某段内容在最新一轮 iteration_summary 中没有被
  正向引用（即删除不会降分），标记为候选删除。removed_lines > 0 是期望状态。

【如果回归】
{上一轮存在 regressions 时附加}：约束——不得降低 task-<X> 的分数。

完成后输出 OSR 到 work_path/osr.json（遵循 osr-skill-writer.schema.json）。
```

### 步骤 3：校验 OSR + overfit_check

```bash
# OSR 结构校验
python3 scripts/osr_validate.py --agent skill_writer --input <work_path>/osr.json

# 知识溯源校验
python3 scripts/overfit_check.py --workspace workspace/ \
    --changes-file <work_path>/osr.json
# 返回：{ok, unmatched_refs: [], task_id_leaks: [], correlation_low: []}
```

任意校验失败时，Orchestrator append `osr_rejected` 事件，以修正指令（引用失败的具体 change_id）重新 spawn。最多重试 2 次后视为 failed，触发 investigator。

### 步骤 4：Git 快照

```bash
cd workspace && git add -A && git commit -m "iter-<N>: <描述>" && git tag skill-v<N>
```

PostToolUse hook 会自动把 `git tag skill-v*` 记录为 `snapshot_created` 事件（P10 保证）。

---

## 全链路流程图

```
Orchestrator (L1 only, Markov)
      │
      ├──[worktree_helper]─→ workspace/.worktrees/runner-with-iterN-tX/
      │                        ├── repo/
      │                        ├── skills/
      │                        └── .isolation.json
      │
      ├──[Task, subagent_type=runner]─→ Runner (in worktree)
      │      │       writes → osr.json, answer.txt
      │      └───── OSR returns ─→ osr_validate → OK
      │
      ├──[subagent_log count-by-uuid]─→ 真实 tool count 或 null
      │
      ├──[blind_eval.py]─→ blinded-eval-results.json + blind-mapping.json
      │
      ├──[worktree_helper × K]─→ .worktrees/grader-iterN-g1..gK/
      │                             ├── blinded-eval-results.json (copied, identical)
      │                             ├── eval-tasks.json (copied, identical)
      │                             └── .isolation.json  (skills/state excluded)
      │
      ├──[Task × K (parallel), subagent_type=grader]─→ K independent Graders
      │      └── each → osr.json + blind-grader-scores.json
      │
      ├──[aggregate_grades.py]─→ blind-grader-scores.json (majority vote + median)
      │                          + ensemble-metrics.json (agreement + disagreement_tasks)
      │
      ├──[deblind_and_score.py]─→ iteration-summary.json + osr-grader-digest.json
      │                             + events.jsonl append
      │
      ├──[Task, subagent_type=skill_writer]─→ Skill Writer
      │      └── OSR with changes_applied + knowledge_source_refs
      │
      ├──[osr_validate + overfit_check]─→ OK or reject
      │
      └──[git tag skill-v<N>]─→ PostToolUse hook auto-logs event
```

---

## 附：`subagent_log.py` 返回值与 toolUseCount 合法性

| 来源 | 语义 | Orchestrator 动作 |
|------|------|------------------|
| `source=sidechain_log, count=N` | 权威计数（同步子代理） | 直接写入 `tool_count_*_from_log` |
| `source=not_available, reason=async_not_in_log` | 异步子代理未记录 | 用 Runner 自报 + 在 OSR 追加 anomaly |
| `source=not_available, reason=tool_use_id_not_found` | UUID 错误或日志损坏 | 视为 Runner 失败，重跑 |

`invariant_check.py` 会定期扫描 iter 间 tool count 的方差与相关性。即使每次单独的 count 不可验证，跨迭代的模式（如 iter-N 与 iter-N+1 完全相同）会触发 guardrail。这是 P03（tool count 伪造）的**双层防御**：能验证则验证，不能验证则统计学保底。
