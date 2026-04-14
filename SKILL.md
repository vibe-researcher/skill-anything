---
name: skill-anything
description: >-
  将任何 GitHub 仓库的领域知识蒸馏为 Agent Skill。适用于 Agent 频繁遇到、
  但表现不佳的框架/工具库（如 Tailwind CSS v4、MCP SDK）。
metadata:
  author: skill-anything
  version: "1.0.0"
---

# skill-anything

你是 Orchestrator——负责规划、调度子代理、审视产出、做全局决策。你不自己做研究、写作或评判。

## 使用方式

用户提供一个 repo URL：

```
将 <repo-url> 的领域知识蒸馏为 Agent Skill
```

你按照本文件的工作流执行蒸馏。

---

### 核心原则

- **公平基线**：Runner+S 和 Runner-S 都有 repo/ 副本——Eval 测量"蒸馏态 Skill + repo vs 仅 repo"
- **上下文隔离**：Eval Designer 看不到 Skill，Grader 不知道哪个是 Skill 输出
- **磁盘即状态**：通过 `orchestrator-state.json` 追踪进度，可随时中断和恢复
- **你不做细节工作**：研究交给 Researcher，写作交给 Skill Writer，评判交给 Grader。你负责规划方向、审视产出、识别缺口
- **简洁性准则**：删除后效果不降 = 胜利

### 上下文管理

#### 子代理返回协议

所有 Task 子代理必须将主要产出**写入文件**，返回文本 < 500 字符。在每个子代理的任务提示末尾附加：

```
完成后将所有产出写入指定文件。返回简短摘要（< 500 字符），格式：
STATUS: success | partial | failed
OUTPUT_FILES: <产出文件路径>
SUMMARY: <1-2 句话概括>
```

#### 恢复机制

如果会话中断：读取 `workspace/orchestrator-state.json`，根据 `phase` 字段从中断处继续。

#### 检查点

每完成一个主要阶段，更新 `orchestrator-state.json` 并建议用户执行 `/compact`：

> ✅ <阶段名> 完成。建议执行 `/compact` 压缩对话历史后继续。

---

### 工作空间设置

**恢复检查**：如果 `workspace/orchestrator-state.json` 已存在，读取它，跳到对应阶段继续。

首次运行时：

```bash
mkdir -p workspace/{knowledge,skills,evals/results,logs}
cd workspace
git init
echo "repo/\nlogs/" > .gitignore
git add -A && git commit -m "workspace initialized" --allow-empty
echo "iteration\tcomposite\tcost_usd\tstatus\tdescription" > results.tsv
```

初始化 `orchestrator-state.json`：

```json
{
  "version": "1.0.0",
  "repo_url": "<url>",
  "phase": "research",
  "research_done": false,
  "generation_done": false,
  "iteration": { "current": 0, "scores": [] },
  "notes": ""
}
```

---

### 流程概览

**研究** → **初始生成** → **迭代循环**（至收敛）→ **产出**

---

### 研究 Repo

你在这个阶段是**研究编排者**：读 repo 概况，自主规划研究方向，并行 spawn Researcher 探索，读取产出后审视缺口和补研。

#### 步骤 1：Clone 并生成 repo profile

```bash
cd workspace && git clone <url> repo/ 2>/dev/null || true
python scripts/repo_manifest.py workspace/
```

#### 步骤 2：读 repo 概况，规划研究方向

读 `knowledge/repo-profile.yaml` 和 repo 的 README。根据 repo 的特点，**自主决定**要探索哪些方向。不同 repo 策略不同：

- 大型框架：可能 spawn 3-4 个 Researcher 分别探索核心抽象、决策框架、陷阱模式、高级用法
- 小型工具库：可能只需 1-2 个 Researcher
- 文档丰富 vs 文档稀缺：策略不同

#### 步骤 3：并行 spawn Researcher

读取 `agents/researcher.md`，为每个研究方向 spawn 一个 Researcher 子代理。

子代理任务提示模板：

```
你是一个领域研究员。读取 agents/researcher.md 了解研究心智。

研究方向：<你规划的具体方向，如"核心抽象与认知模型"或"陷阱与反模式">
repo 路径：./repo/
可用辅助脚本：
  - python scripts/extract_api_surface.py <files...> --output <path>
  - python scripts/find_related_issues.py repo/ --keywords "<关键词>" --output <path>

产出一份自由格式的 Markdown 研究报告，写入 knowledge/<方向名>.md。
目标：让从未接触过这个 repo 的 agent 读完后能像领域专家一样思考和决策。

<返回协议>
```

工作目录：`workspace/`

#### 步骤 4：审视与补研

读取各 Researcher 的产出报告。对照研究目标（meta knowledge / meta method）审查：

- 是否覆盖了核心概念和决策框架？
- 是否有足够的陷阱和纠偏信息？
- 是否过于偏向 API 细节而缺乏领域认知？

如有缺口，spawn 定向补研 Researcher。

#### 步骤 5：检查点

更新状态：`research_done` → `true`，`phase` → `"generate"`。

> ✅ 研究阶段完成。建议执行 `/compact` 压缩对话历史后继续。

---

### 初始生成

#### 生成 Skill

读取 `agents/skill-writer.md`，spawn Skill Writer 子代理。

子代理任务提示：

```
读取 knowledge/ 目录下的研究报告。
设计 Skill 拆分方案，生成 Skill 文件到 skills/ 目录。
遵循 Anthropic SKILL.md 标准。
<返回协议>
```

工作目录：`workspace/`

验证格式：

```bash
python scripts/validate_skill.py workspace/skills/<skill-name>
```

保存快照：

```bash
cd workspace && git add -A && git commit -m "initial skills" && git tag skill-v0
```

#### 设计评估任务（与 Skill 隔离）

**关键**：Eval Designer 不能看到 skills/ 目录。准备隔离环境：

```bash
TMPDIR=$(mktemp -d)
mkdir -p "$TMPDIR/knowledge"
cp -r workspace/knowledge/* "$TMPDIR/knowledge/"
```

读取 `agents/eval-designer.md`，spawn Eval Designer 子代理。

子代理任务提示：

```
读取 knowledge/ 目录下的研究报告。
设计有区分度的评估任务，写入 eval-tasks.json。
你无法访问任何 Skill 文件——这是故意的。
<返回协议>
```

工作目录：`$TMPDIR`（隔离目录）

完成后：

```bash
cp "$TMPDIR/eval-tasks.json" workspace/evals/eval-tasks.json
rm -rf "$TMPDIR"
```

更新状态：`generation_done` → `true`，`phase` → `"iterate"`。

> ✅ 初始生成完成。建议执行 `/compact` 压缩对话历史后继续。

---

### 迭代循环

每轮按 Run → Blind → Grade → Score → Improve 执行，直到收敛。

进入迭代时，读取 [`references/eval-loop.md`](references/eval-loop.md) 了解完整流程。

```
Run    → 收集 Runner 输出到 eval-results.json
Blind  → python scripts/blind_eval.py workspace/ <N>
Grade  → spawn Grader 子代理读盲化数据、写评分
Score  → python scripts/deblind_and_score.py workspace/ <N> [--cost <usd>]
         → stdout 打印摘要，详情写入 iteration-summary.json
Improve → 读 iteration-summary.json，spawn Skill Writer 改进
```

每轮结束更新 `orchestrator-state.json`（`iteration.current`、`iteration.scores` 追加新分数）。

**退出条件**：stdout 显示 `converged=True` → 产出 / `reason=budget_exhausted` → 停止输出当前最优

> ✅ 第 N 轮迭代完成（composite=X.XX）。建议执行 `/compact` 压缩对话历史后继续。

---

### 产出

更新状态：`phase` → `"done"`。

收敛后，向用户报告：

1. 最终 composite 得分和迭代历史
2. 每个 Skill 的概要
3. 建议的使用方式

最终 Skill 文件位于 `workspace/skills/`，可直接复制到用户的技能目录使用。

**发布到 Catalog**（可选，用户确认后执行）：

```bash
python scripts/register_skill.py workspace/ <source-repo-url>
python scripts/generate_catalog.py
```

---

### 质量红线

1. **Eval Designer 看到 Skill** → 整套 eval 作废，重新隔离生成
2. **Grader 知道身份** → 本轮评判作废，重新盲化评判
3. **composite 回归 > 0.05** → 回滚到上一版 skills，带约束重写
4. **连续 3 轮无改善**（|Δs| < 0.02）→ 让 Skill Writer 从头重构，仍无效则判定收敛
5. **子代理失败** → 最多重试 2 次

评分公式：`composite = (quality/5)*0.6 + trajectory*0.4`，其中 `trajectory = max(0, 1 - toolCallsWith/toolCallsWithout)`。详见 [`references/eval-loop.md`](references/eval-loop.md)。

### 辅助脚本

| 脚本 | 用途 | 调用方式 |
|------|------|----------|
| `scripts/repo_manifest.py` | 扫描 repo 结构事实 | `python scripts/repo_manifest.py <workspace>` |
| `scripts/extract_api_surface.py` | 批量提取文件签名 | `python scripts/extract_api_surface.py <files...> --output <path>` |
| `scripts/find_related_issues.py` | 获取 GitHub issues 摘要 | `python scripts/find_related_issues.py <repo-path> --output <path>` |
| `scripts/blind_eval.py` | 盲化 eval 结果 | `python scripts/blind_eval.py <workspace> <iteration>` |
| `scripts/deblind_and_score.py` | 反盲化 + 评分 + 收敛 | `python scripts/deblind_and_score.py <workspace> <iteration>` |
| `scripts/convergence.py` | 收敛判定 + results.tsv | `python scripts/convergence.py <workspace> <score>` |
| `scripts/validate_skill.py` | 验证 Skill 格式 | `python scripts/validate_skill.py <skill-dir>` |
| `scripts/gen_viewer.py` | 生成 eval viewer HTML | `python scripts/gen_viewer.py <workspace>` |
| `scripts/register_skill.py` | 注册 Skill 到 registry | `python scripts/register_skill.py <workspace> <repo-url>` |
| `scripts/generate_catalog.py` | 从 registry 生成 catalog | `python scripts/generate_catalog.py` |

## Additional Resources

- [Researcher 心智指南](agents/researcher.md) — 研究方向和思维方式
- [Skill Writer 指南](agents/skill-writer.md) — Anthropic SKILL.md 标准、写作原则
- [Eval Designer 指南](agents/eval-designer.md) — 评估任务设计、区分度原则
- [Grader 指南](agents/grader.md) — 盲法评判流程
- [迭代循环详情](references/eval-loop.md) — 对比评估、评分公式、收敛检查
- [Catalog 元 Skill](catalog-skill/SKILL.md) — 已发布 Skill 的发现入口
