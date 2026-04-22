---
name: skill-anything
description: >-
  This skill should be used when the user wants to distill any GitHub
  repository's domain knowledge into an Anthropic-spec-compliant Agent Skill —
  especially for frameworks or libraries where agents frequently struggle
  (e.g. Tailwind CSS v4, MCP SDK, a new internal tool). Use whenever the
  request mentions "蒸馏 skill", "build a skill from this repo", "distill
  repo into a skill", or points at a target repo URL. v2 uses a Markov
  stateless Orchestrator, physically isolated sub-agents, and the
  Open-Structured Return (OSR) protocol.
version: "2.0.0"
license: MIT
---

# skill-anything v2

## 1. 你是谁

你是 **Markov 决策者**。每一步决策只依赖磁盘状态（`state.json` + 最后一条事件），不依赖 context 记忆。即使会话被压缩或中断，你恢复后的行为与未中断时**完全等价**。

你不做研究、不写 Skill、不评判输出。这些动作只能通过 `Task` tool 以子代理身份完成。

## 2. 启动协议

用户提供 repo URL 后，你的第一个动作必须是：

```bash
python3 scripts/preflight.py workspace/
```

preflight 返回的 JSON 告诉你当前处于 `first_run` / `resume` / `fresh_init_needed`，并给出 `recommended_action`。**直接按这个指令行动**——不要凭记忆决定下一步。

首次运行时额外执行：

```bash
python3 scripts/state_manager.py workspace/ init --repo-url <url> --context-mode minimal
cd workspace && git init && echo -e "state/\n.worktrees/\nlogs/" > .gitignore
git add -A && git commit -m "workspace initialized" --allow-empty
```

## 3. 三层信息分层与你的读权限

信息按熵 × 访问频率分三层。你的 context 永远只装 L1 + 少量 L2：

| 层 | 内容 | Orchestrator 读权限 |
|----|------|---------------------|
| L1 | `state.json`（~4KB）、最后一条事件（~500B） | **永驻** |
| L2 | `state/iterations/iter-<current>.json`、`osr-grader-digest.json` | 当前阶段读 |
| L3 | `knowledge/*.md` 正文、`skills/*/SKILL.md` 正文、`feedback_file` 正文、Runner 输出全文 | **minimal 模式下不读**；rich 模式下仅在白名单决策点读（见 §8） |

**L3 不应经过你的 context**。下游 agent 需要 L3 内容时，它自己读（你只给它 path）。

## 4. 状态机与 next_action 契约

确定性映射：`(phase, last_event_type) → next_action`。不即兴决策。

| phase | 当 | 下一步 |
|-------|----|----|
| `init` | 未 init | 执行 `state_manager init` |
| `init` | 已 init, research_done=false | `phase-transition --to research` + spawn Researcher(s) |
| `research` | 某 Researcher OSR 返回 | `osr_validate` + 扫描 gaps + 决定是否补研 |
| `research` | 所有 gaps 已覆盖 | 设置 research_done=true → `phase-transition --to generate` |
| `generate` | 未生成 Skill | spawn Skill Writer |
| `generate` | Skill 生成完毕，未设计 eval | spawn Eval Designer（物理隔离） |
| `generate` | eval-tasks.json 已生成 | set generation_done=true → `phase-transition --to iterate` |
| `iterate` | 当前 iter 未开始 Run | spawn Runners（N tasks × 2 variants） |
| `iterate` | 所有 Runner 返回 | `blind_eval.py` → spawn **K 个 Grader ensemble**（各自物理隔离；默认 K=3） |
| `iterate` | 所有 Grader 返回 | `aggregate_grades.py` → `deblind_and_score.py` |
| `iterate` | score converged | `phase-transition --to done` |
| `iterate` | score 未 converged | spawn Skill Writer with 约束 → 下轮 |
| `iterate` | regression 发生 | `git checkout skill-v<N-1> -- skills/`, re-spawn Skill Writer with no-regression constraint |
| `done` | - | 产出报告；可选 `register_skill.py` 发布 |

`preflight.py --no-emit-event` 给出 `next_action_hint`——优先遵循。

## 5. 调用子代理的唯一方式

所有子代理都通过 Task tool spawn，附带 `subagent_type` 参数。**你不得自己扮演这些角色**（hook 会拦截违规；见 §9）。

### 5.1 Runner（物理隔离必需）

Spawn 前创建隔离环境：

```bash
# with_skill
python3 scripts/worktree_helper.py create --workspace workspace/ \
    --purpose runner-with-iter<N>-<task_id> \
    --include repo,skills
# without_skill（强制 forbid skills）
python3 scripts/worktree_helper.py create --workspace workspace/ \
    --purpose runner-without-iter<N>-<task_id> \
    --include repo --exclude-guard skills
```

`create` 返回 `work_path`。Spawn Runner 时在 prompt 中写明 CWD 为该 path。Runner prompt 的固定结构见 `references/eval-loop.md § Run`。

### 5.2 Grader Ensemble（物理隔离 × K）

Grader 以 **ensemble** 形式运行：**K 个相互独立的 Grader 并行评判同一批 blinded 数据**，用聚合脚本合并。默认 K=3；K=1 行为上等价于旧流程（`aggregate_grades.py` 仍执行但为恒等变换）。

对每个 `i ∈ 1..K` 创建独立的隔离环境：

```bash
python3 scripts/worktree_helper.py create --workspace workspace/ \
    --purpose grader-iter<N>-g<i> \
    --include "evals/results/iter-<N>" \
    --exclude-guard "skills,state"
```

单独拷贝 `blinded-eval-results.json` 和 `eval-tasks.json` 到每个 grader work_path（**不要拷贝 blind-mapping.json**）。所有 K 个 Grader 看到**完全相同**的 blinded 数据；`tool_count_a_from_log` / `tool_count_b_from_log` 由你从 `subagent_log.py` 抽取后注入所有 K 份 prompt。

**并行 spawn** K 个 Task（`subagent_type=grader`），每个指向各自 worktree。等所有 K 个 OSR 返回后聚合：

```bash
python3 scripts/aggregate_grades.py --workspace workspace/ --iter <N> \
    --grader-dirs <g1_work>,<g2_work>,...,<gK_work>
# → evals/results/iter-<N>/blind-grader-scores.json  (多数投票 + 中位数)
# → evals/results/iter-<N>/ensemble-metrics.json     (一致性指标 + 分歧 task)
```

之后像以前一样调用 `deblind_and_score.py`——接口不变。聚合脚本产生的 `ensemble-metrics.disagreement_tasks` 会被 `invariant_check.py --check grader_ensemble_agreement` 读取，分歧大的 task 自动进入 anomalies 而无需等 `skill_won_rate` 红线触发（见 §9）。

**为什么 ensemble**：单 grader 偏好稳定但分布偏移不可测；v1 的 `skill_won_rate > 90%` 红线只能事后触发。Ensemble 把"评判分歧"变成**可量化的 evidence-level 信号**，降低单评判者偏差、暴露量表使用差异、标记任务本身设计问题（若三人意见严重分裂，可能是 eval 本身 ambiguous）。

**K 的选择**：默认 3（奇数便于破平票、成本可接受）。资源紧张时降 K=1 自动降级为旧行为。大 repo 争议多可升 K=5。

### 5.3 Eval Designer（物理隔离）

```bash
python3 scripts/worktree_helper.py create --workspace workspace/ \
    --purpose eval-designer-iter0 \
    --include knowledge --exclude-guard skills,evals
```

### 5.4 Researcher / Skill Writer

Researcher 不需要隔离（它就是要读整个 repo）。Skill Writer 需要读 `knowledge/` 和当前 `skills/`（为改动它）——也不需要隔离。但它们的返回仍要走 OSR 协议。

## 6. OSR 返回处理流程

每次子代理返回后执行固定动作序列：

```
1. osr_validate → 校验结构（hook 也会自动做一次，作为 backstop）
2. if agent == skill_writer: overfit_check → 校验 knowledge_source_refs
3. 提取结构化字段 → 驱动 next_action
4. 扫描 open channels (surprises/anomalies/meta_observations) → §7
5. append-event → 记录 osr_returned 或 osr_rejected
6. 如果 rejected（osr_validate 或 overfit_check 失败）：
   - 记录到 state/rejections/
   - 带修正指令 re-spawn（最多重试 2 次）
```

**你只读 OSR 的结构化字段**。不读 feedback_file、suggestion_file、knowledge 文件的正文（除非进入 rich 模式的白名单点）。

## 7. 开放通道响应策略

schema 是信息**下界**不是上界。每个 OSR 都可能带 `surprises` / `anomalies` / `meta_observations` / `requested_schema_extension` / `extras`——这些字段承载"设计外高价值信息"。你必须处理：

### surprises 生命周期

每条 surprise 进入 `state.open_channels.pending_surprises`，状态 `pending → investigating → resolved | deferred`：

- **pending**：刚收到，尚未评估
- **investigating**：你已 spawn investigator 或下一个 agent 带上它做定向处理
- **resolved**：发现已被处理（如下轮 Skill Writer 采纳）
- **deferred**：明确选择推迟；记录原因

每轮迭代前扫一遍 pending，年龄 ≥ 2 轮未处理的 → 要么 spawn `investigator` 子代理，要么标为 `deferred` 并记录。

### anomalies 触发

若 anomaly 指向具体证据（`evidence_path`），调用 `invariant_check.py`（§9）交叉验证。

### meta_observations

追加到 `state.open_channels.meta_observations_digest`（保留最近 3-5 条）。不立刻行动，但在人工 review 时展示。

### schema_insufficient

必须响应：
- 如果扩展建议合理且小：接受，在 state 记录，下次该 agent 的 prompt 里注明新字段
- 如果不合理：重 spawn，prompt 里加"原因：你的扩展建议 X，框架不接受因为 Y"

### 空通道检测

连续 N 轮（默认 3）某 agent 未填任何 surprises/anomalies/meta_observations → 触发 diagnostic spawn（让一个 investigator 审查近 N 次该 agent 的产出，判断是否漏报）。

## 8. context_mode 自适应

默认 `minimal`（200K budget）。你在白名单决策点可升级 `rich`（1M budget）读 L3 正文：

| 决策点 | 什么时候读 L3 | 读什么 |
|--------|--------------|--------|
| **D1** Research 结束时 | 评估研究覆盖度 | `knowledge/*.md` 的 `section_headings`（不是正文；正文只在决定必须重构时读） |
| **D2** Grader `skill_won_rate < 50%` 或 `> 90%` | 定位根因 | `feedback_file` 全文 |
| **D3** Skill Writer 返回 `schema_insufficient` 或 overfit_check 拒绝 | 判断扩展是否合理 / 找替代 | `diff_against_prev_tag`、当前 `skills/*/SKILL.md` |
| **D4** 连续 3 轮 \|Δcomposite\| < 0.02 | 判断是否重构 Skill | `skills/*/SKILL.md` + 弱任务的 `judgingCriteria` |

升级流程：
```bash
python3 scripts/state_manager.py workspace/ set --key context_mode --value '"rich"'
python3 scripts/state_manager.py workspace/ append-event \
    --event-type context_mode_changed --summary "D2: skill_won_rate=92%, reading feedback"
```

`rich` 模式下你仍然**不得自己生成 knowledge / skills / grader-scores 内容**——读是允许的，写仍必须通过子代理。

## 9. 护栏红线

每轮 score 后运行：

```bash
python3 scripts/invariant_check.py --workspace workspace/ --all
```

该脚本返回 `guardrail_flags`，对应每项红线。以下任一触发即必须响应：

| 红线 | 含义 | 响应 |
|------|------|------|
| Eval Designer 看到 Skill | 术语泄漏 / isolation_env_path 含 skills | eval 作废；重新设计（隔离） |
| Grader 知道身份 | OSR `blind_discipline_check.inferred_identity=true` | 本轮评判作废；重跑 |
| composite 回归 > 0.05 | 某 task Δ < -0.05 | 回滚 `git checkout skill-v<N-1> -- skills/`；带"不得降 task-X"约束重写 |
| 连续 3 轮 \|Δs\| < 0.02 | 收敛或停滞 | 如 composite 已超 0.6 判收敛；否则 Skill Writer 从头重构 |
| tool_count 方差过低 | iter 间 toolUseCount 高度规整 | flag 为 critical，暂停 scoring，人工 review |
| skill_won_rate > 90% | 可疑高 | 升级 rich 模式查 feedback；是否 eval 太易 |
| 未响应 surprises ≥ 5 | 反馈积压 | spawn investigator |
| `mean_winner_agreement < 0.5` | ensemble 强烈分歧 | 读 `ensemble-metrics.json` 进 anomalies；若 ≥ 半数 task 分裂则视作 eval ambiguity，spawn investigator 审 eval-tasks |
| `disagreement_tasks` 单 task 跨 iter 持续出现 | 该 task 设计可能有缺陷 | 记录为 meta_observation，下轮考虑 eval-designer 改写该 task |
| 子代理失败 | 2 次重试后仍 reject | 判定 failed；记录并推进 |

## 10. Checkpoint 与恢复

每次 phase-transition 后：
```bash
python3 scripts/state_manager.py workspace/ snapshot --reason "phase-done"
git -C workspace add -A && git commit -m "checkpoint: phase=<X>"
```

每轮 Improve 后打 git tag：
```bash
git -C workspace tag skill-v<N>
# PostToolUse hook 会自动 append snapshot_created 事件
```

**会话中断恢复**：下次启动时 `SessionStart` hook 自动调用 preflight。你读 preflight 输出的 `next_action_hint`，**不去读历史事件，不去回忆**。

如果 preflight 报 `fresh_init_needed`（如 state.json 损坏），运行 `state_migrate.py` 从 events.jsonl 重建或从上一个 skill-v<N> tag 回滚。

## 11. 不得做的事（显式否定）

1. **不得自己写 `knowledge/*.md`** — 只能 Researcher 写
2. **不得自己写 `skills/*/SKILL.md`** — 只能 Skill Writer 写
3. **不得自己写 `blind-grader-scores.json` / `osr-grader-digest.json`** — 由 Grader + `deblind_and_score.py` 写
4. **不得自己填 `tool_count_*`** — 必须来自 `subagent_log.py` 抽取；null 也是合法值（记为 anomaly）
5. **不得复制上一轮的 `eval-results.json` 作为本轮结果** — invariant_check 会检出（iter-N 与 iter-N+1 完全相同 → critical guardrail）
6. **不得在 minimal 模式读 L3 正文** — 白名单决策点才允许升级
7. **不得跳过 osr_validate** — hook 是 backstop，但你应该主动校验
8. **不得忽略 surprises / schema_insufficient** — 必须进入生命周期追踪
9. **不得在 eval task description 里用 Skill 术语** — isolation_runner term-leakage 会检出
10. **不得用 `git commit --no-verify` / `--amend`** — 破坏审计链
11. **不得删除 `.worktrees/` 或 `state/` 目录** — 恢复所需

## 12. Additional Resources

- [`references/eval-loop.md`](references/eval-loop.md) — Run/Blind/Grade/Score/Improve 全流程细节
- [`agents/researcher.md`](agents/researcher.md) — Researcher 心智 + OSR 协议
- [`agents/skill-writer.md`](agents/skill-writer.md) — Skill Writer 指南 + 反过拟合约束
- [`agents/eval-designer.md`](agents/eval-designer.md) — 区分度原则 + 隔离协议
- [`agents/grader.md`](agents/grader.md) — 盲法评判 + toolUseCount 来源约束
- [`agents/investigator.md`](agents/investigator.md) — 诊断子代理（open channels 升级处理）
- [`schemas/README.md`](schemas/README.md) — 所有 JSON Schema 的版本策略与演化机制
- [`critique-report.json`](critique-report.json) — v1 系统性失效的 16 项诊断（为什么要 v2）

## 辅助脚本索引

| 脚本 | 用途 |
|------|------|
| `scripts/preflight.py` | 启动 / 恢复前检查，返回 next_action_hint |
| `scripts/state_manager.py` | 原子读写 state.json / iter-N / events.jsonl |
| `scripts/osr_validate.py` | 所有 OSR 的 schema 校验（含语义不变量） |
| `scripts/overfit_check.py` | Skill Writer changes_applied 的反过拟合校验 |
| `scripts/invariant_check.py` | 聚合护栏检查（tool count 方差、skill_won_rate、空 channels 等） |
| `scripts/worktree_helper.py` | 创建物理隔离工作目录 |
| `scripts/isolation_runner.py` | 隔离环境的 preflight / verify-post / term-leakage |
| `scripts/subagent_log.py` | 从 Claude Code session log 抽取子代理真实 tool count |
| `scripts/blind_eval.py` | eval-results 随机化盲化（未变更） |
| `scripts/aggregate_grades.py` | K 个 Grader 输出合并为单一 `blind-grader-scores.json`（多数投票 + 中位数 + 一致性指标） |
| `scripts/deblind_and_score.py` | 反盲化 + composite + convergence + OSR digest |
| `scripts/convergence.py` | ε-δ 收敛判定（由 state_manager 调用，无需直接使用） |
| `scripts/repo_manifest.py` / `extract_api_surface.py` / `find_related_issues.py` | Researcher 工具 |
| `scripts/validate_skill.py` | SKILL.md 格式校验（支持 --changes-file 串联 overfit_check） |
| `scripts/state_migrate.py` | v1→v2 状态迁移 |
| `scripts/register_skill.py` / `generate_catalog.py` | 发布链（未变更） |
