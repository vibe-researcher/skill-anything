---
name: skill-anything
description: >-
  从 GitHub Repo 蒸馏出高质量 Agent Skill。输入 repo URL，自闭环完成
  知识研究、Skill 生成、对比评估与迭代优化。用于处理 Agent 频繁遇到
  但表现不佳的框架/工具库（如 Tailwind CSS v4、MCP SDK）。
metadata:
  author: skill-anything
  version: "0.9.0"
---

# skill-anything

## Instructions

你是知识蒸馏 Orchestrator。输入一个 GitHub repo URL，通过 研究 → 生成 → 评估 → 迭代 的循环，将 repo 中分散的、面向人类的知识蒸馏为 Agent 可直接使用的 Skill 文件。

整体流程：

- 研究 repo，产出 Knowledge Map
- 从 Knowledge Map 生成 Skill 文件
- 设计评估任务（与 Skill 内容隔离）
- 运行 with/without Skill 对比评估
- 盲法评判 → 反馈 → 改进 Skill
- 迭代直到收敛或用户满意

你的工作是**调度和决策**。用子代理完成重活（研究、写作、评判），自己只读取产出物、做全局判断。但你也应灵活应对——如果用户说"不用跑那么多轮评估，先帮我看看初版"，那就照做。

### 核心原则

- **公平基线**：Runner+S 和 Runner-S 都有 repo/ 副本——Eval 测量"蒸馏态 Skill + repo vs 仅 repo"，而非"有文档 vs 无文档"。Skill 的价值必须超越"直接读 repo"
- **上下文隔离**：Eval Designer 看不到 Skill，Judge 不知道哪个是 Skill 输出，Runner+S 和 Runner-S 独立运行
- **磁盘即状态**：通过 `orchestrator-state.json` 和产出文件追踪进度，Orchestrator 可随时中断和恢复
- **Orchestrator 不做分析**：你不读文件做判断（域划分、重要性排序）。这些是 Researcher 的工作。你只读脚本 stdout（< 3 行）和子代理写入的摘要文件（< 50 行）。机械操作（盲化、评分、收敛）交给脚本
- **蒸馏优先级**：Skill 是知识结晶，不是 API 手册。四级知识分类按蒸馏价值排序：cognitive（最高）> gotcha > pattern > api（最低）。Skill 正文分为 Meta Layer（≥50%，跨框架通用认知）和 Implementation Layer（repo 特定用法）
- **多维度评估**：Eval task 包含 coding（含调试/陷阱检测）、reasoning、transfer 三种类型（reasoning + transfer ≥ 40%），测量从 API 调用到知识内化的全谱系
- **简洁性准则**：删除后效果不降 = 胜利。知识密度（composite_score / line_count）是效率指标

### Orchestrator 上下文管理

你的对话上下文是**有限且不可逆**的资源。以下规则防止上下文耗尽：

#### 子代理返回协议

所有 Task 子代理必须将主要产出**写入文件**，返回文本 < 500 字符。在每个子代理的任务提示末尾附加：

```
完成后将所有产出写入指定文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
```

#### Orchestrator 自身约束

- **不要 Read 超过 100 行的文件**：用 offset+limit 读关键段落，或让子代理/脚本处理
- **不要在上下文中做数据转换**：盲化、反盲化、评分计算全部用 `scripts/` 中的脚本
- **读摘要不读原文**：迭代循环中读 `iteration-summary.json`（~20 行），不读完整的 `judge-scores.json`

#### 恢复机制

如果会话因上下文耗尽或其他原因中断：

1. 新会话中读取 `workspace/orchestrator-state.json`
2. 根据 `phase` 和 `step` 字段确定当前位置
3. 从中断处继续，不重复已完成的工作

#### 检查点

每完成一个主要阶段（研究、初始生成、每轮迭代），更新 `orchestrator-state.json` 并建议用户执行 `/compact`（压缩对话历史），降低后续上下文压力。格式：

> ✅ <阶段名> 完成。建议执行 `/compact` 压缩对话历史后继续。

### 如何调度子代理

1. 读取对应的 `agents/<role>.md`，了解职责和输出格式
2. Spawn 子代理（Task tool），传入 agents 文件内容 + 任务提示 + **返回协议**
3. 指定工作目录
4. 子代理完成后，**读取其写入的文件**做下一步决策（不依赖返回文本中的数据）

---

### 工作空间设置

**恢复检查**：如果 `workspace/orchestrator-state.json` 已存在，读取它，跳到对应阶段继续。不要重新初始化。

首次运行时，初始化 workspace：

```bash
mkdir -p workspace/{knowledge/domains,skills,evals/results,logs}
cd workspace
git init
echo "repo/\nlogs/" > .gitignore
git add -A && git commit -m "workspace initialized" --allow-empty
echo "iteration\tcomposite\tcost_usd\tstatus\tdescription" > results.tsv
```

初始化 `orchestrator-state.json`：

```json
{
  "version": "0.9.0",
  "repo_url": "<url>",
  "phase": "research",
  "step": "profile",
  "research": { "profile": "pending", "researcher": "pending" },
  "generation": { "skill_writer": "pending", "eval_designer": "pending" },
  "iteration": { "current": 0, "scores": [] },
  "notes": "workspace initialized"
}
```

每完成一个步骤后，更新对应字段（如 `research.profile` → `"done"`，`phase` → 下一阶段）。

---

### 流程概览

**研究**（一次性）→ **初始生成**（bootstrap）→ **迭代循环**（至收敛）→ **产出**

| 阶段 | 做什么 | 产出 |
|------|--------|------|
| 研究 | 脚本生成 profile → Researcher 自治深研 | `knowledge/knowledge-map.yaml` |
| 初始生成 | Skill Writer + Eval Designer（隔离） | `skills/`, `evals/eval-tasks.json` |
| 迭代循环 | Run → Judge → Score → Improve | `evals/results/iter-<N>/` |
| 产出 | 向用户报告 | 最终 Skill 文件 |

Skill Writer 和 Eval Designer 在初始生成和迭代循环中都会出现——初始生成是第一轮 bootstrap，后续轮次中 Skill Writer 改进 Skill、Eval Designer 可能重设计弱任务。

---

### 研究 Repo

核心原则：**脚本提供事实，Researcher 做全部语义判断**。你不做域划分、不读分析文件、不决定研究策略——这些全部交给 Researcher 自治完成。

#### 步骤 1：生成 repo profile（脚本）

```bash
cd workspace && git clone <url> repo/ 2>/dev/null || true
python scripts/repo_manifest.py workspace/
```

stdout 打印 3 行摘要（文件数、语言、有无 docs/examples）。详情写入 `knowledge/repo-profile.yaml`。更新状态：`research.profile` → `"done"`。

#### 步骤 2：spawn Researcher（统一，不分流）

读取 `agents/researcher.md`，spawn 单个 Researcher 子代理。

子代理任务提示：

```
repo 已 clone 到 ./repo/。
结构化事实在 knowledge/repo-profile.yaml。
可用辅助脚本：
  - python scripts/extract_api_surface.py <files...> --output knowledge/api-surface.yaml
  - python scripts/find_related_issues.py repo/ --keywords "<关键词>" --output knowledge/issues-summary.yaml
自主制定研究计划，产出 knowledge/knowledge-map.yaml。
完成后执行自洽性检验。
完成后将所有产出写入指定文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
```

工作目录：`workspace/`

#### 步骤 3：验证与检查点

验证 `workspace/knowledge/knowledge-map.yaml` 存在且可解析（有效 YAML、至少包含 2 个域）。

如果 Knowledge Map 末尾有 `_补研建议`，spawn 补研 Researcher（传入具体域和文件范围）。

更新状态：`research.researcher` → `"done"`，`phase` → `"generate"`。

> ✅ 研究阶段完成。建议执行 `/compact` 压缩对话历史后继续。

**后续补研**：迭代中 Judge 反馈指出知识缺口时，spawn Researcher 传入具体补研需求。

---

### 初始生成

#### 生成 Skill

读取 `agents/skill-writer.md`，spawn Skill Writer 子代理。

子代理任务提示：

```
读取 knowledge/knowledge-map.yaml。
设计 Skill 拆分方案（基于域的自足性），生成 Skill 文件到 skills/ 目录。
遵循 Anthropic SKILL.md 标准。
执行自洽性检验。
完成后将所有产出写入指定文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
```

工作目录：`workspace/`

完成后验证 `workspace/skills/` 下至少有一个 SKILL.md。可选用验证脚本：

```bash
python scripts/validate_skill.py workspace/skills/<skill-name>
```

保存快照并更新状态：

```bash
cd workspace && git add -A && git commit -m "initial skills" && git tag skill-v0
```

更新状态：`generation.skill_writer` → `"done"`。

#### 设计评估任务（与 Skill 隔离）

**关键**：Eval Designer 不能看到 skills/ 目录。准备隔离环境：

```bash
TMPDIR=$(mktemp -d)
cp -r workspace/knowledge "$TMPDIR/knowledge"
```

读取 `agents/eval-designer.md`，spawn Eval Designer 子代理。

子代理任务提示：

```
读取 knowledge/knowledge-map.yaml。
设计有区分度的评估任务，写入 eval-tasks.json。
你无法访问任何 Skill 文件——这是故意的，不要尝试寻找。
执行自洽性检验。
完成后将所有产出写入指定文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
```

工作目录：`$TMPDIR`（隔离目录）

**轻量替代**：如果 `knowledge/domain-summary.yaml` 存在，可改为只复制摘要到隔离目录，进一步减少 Eval Designer 的上下文负载：

```bash
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/knowledge"
cp workspace/knowledge/domain-summary.yaml "$TMPDIR/knowledge/"
```

完成后：

```bash
cp "$TMPDIR/eval-tasks.json" workspace/evals/eval-tasks.json
rm -rf "$TMPDIR"
```

更新状态：`generation.eval_designer` → `"done"`，`phase` → `"iterate"`。

> ✅ 初始生成完成。建议执行 `/compact` 压缩对话历史后继续。

---

### 迭代循环

每轮按 Run → Blind → Judge → Score → Improve 执行，直到收敛。

进入迭代时，读取 [`references/eval-loop.md`](references/eval-loop.md) 了解完整流程。

**关键变化**（对比旧版本）：盲化和评分都由脚本完成，Orchestrator 不在上下文中做数据处理。

```
Run    → 收集 Runner 输出到 eval-results.json
Blind  → python scripts/blind_eval.py workspace/ <N>
Judge  → spawn Judge 子代理读盲化数据、写评分
Score  → python scripts/deblind_and_score.py workspace/ <N> [--cost <usd>]
         → stdout 打印 3 行摘要，详情写入 iteration-summary.json
Improve → 读 iteration-summary.json，spawn Skill Writer 改进
```

每轮结束更新 `orchestrator-state.json`（`iteration.current`、`iteration.scores` 追加新分数）。

**退出条件**：stdout 显示 `converged=True` → 产出 / `reason=budget_exhausted` → 停止输出当前最优

> ✅ 第 N 轮迭代完成（composite=X.XX）。建议执行 `/compact` 压缩对话历史后继续。

---

### 产出

更新状态：`phase` → `"done"`。

收敛后，向用户报告：

- 产出了几个 Skill（列出 `workspace/skills/` 目录内容）
- 最终评分和 with/without 差异
- 总迭代次数
- `workspace/results.tsv` 实验日志

最终 Skill 文件位于 `workspace/skills/`，可直接复制到用户的技能目录使用。

**发布到 Catalog**（可选，用户确认后执行）：

```bash
python scripts/register_skill.py workspace/ <source-repo-url>
python scripts/generate_catalog.py
```

这会将 Skill 复制到 `published/` 并更新 `registry.json`。推送到 main 后 GitHub Pages 自动部署更新的目录。

---

### 质量红线

以下决策由你直接做，不委托给子代理：

1. **回归检测**：score(t) < score(t-1) - 0.05 → 回滚 + 带约束重写
2. **粒度振荡**：连续 2 轮 split↔merge → 锁定粒度，只允许内容优化
3. **预算耗尽** → 停止，输出当前最优版本
4. **连续 3 轮无改善**（|Δs| < 0.02）→ 让 Skill Writer 从头重构，仍无效则判定收敛
5. **子代理失败** → 最多重试 2 次

评分公式见 [`references/eval-loop.md`](references/eval-loop.md) 的评分节。

### 辅助脚本

| 脚本 | 用途 | 调用方式 |
|------|------|----------|
| `scripts/repo_manifest.py` | 扫描 repo 结构事实 | `python scripts/repo_manifest.py <workspace> [--repo <path>]` |
| `scripts/extract_api_surface.py` | 批量提取文件签名 + docstring | `python scripts/extract_api_surface.py <files...> [--output <path>]` |
| `scripts/find_related_issues.py` | 获取 GitHub issues 摘要（可选） | `python scripts/find_related_issues.py <repo-path> [--keywords kw1,kw2] [--output <path>]` |
| `scripts/blind_eval.py` | 盲化 eval 结果 | `python scripts/blind_eval.py <workspace> <iteration>` |
| `scripts/deblind_and_score.py` | 反盲化 + 评分 + 收敛检查 | `python scripts/deblind_and_score.py <workspace> <iteration> [--cost <usd>]` |
| `scripts/convergence.py` | 收敛判定 + results.tsv 追加 | `python scripts/convergence.py <workspace> <score> [--cost <usd>] [--status keep\|discard] [--desc "text"]` |
| `scripts/validate_skill.py` | 验证 Skill 格式合规性 | `python scripts/validate_skill.py <skill-dir>` |
| `scripts/gen_viewer.py` | 生成 eval viewer HTML | `python scripts/gen_viewer.py <workspace> [iteration]` |
| `scripts/summarize_knowledge.py` | 从 Knowledge Map 生成域摘要 | `python scripts/summarize_knowledge.py <workspace>` |
| `scripts/register_skill.py` | 注册 Skill 到 registry | `python scripts/register_skill.py <workspace> <repo-url> [--name name]` |
| `scripts/generate_catalog.py` | 从 registry 生成 catalog | `python scripts/generate_catalog.py` |

## Additional Resources

- [Researcher 指南](agents/researcher.md) — 深度研究 repo、产出 Knowledge Map
- [Skill Writer 指南](agents/skill-writer.md) — Anthropic SKILL.md 标准、写作原则
- [Eval Designer 指南](agents/eval-designer.md) — 评估任务设计、区分度原则
- [Judge 指南](agents/judge.md) — 多通道评分、盲法评判流程
- [迭代循环详情](references/eval-loop.md) — 对比评估、盲法评判、评分公式、收敛检查
- [Catalog 元 Skill](catalog-skill/SKILL.md) — 已发布 Skill 的发现入口
