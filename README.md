# skill-anything：从 GitHub Repo 到 Agent Skill 的知识蒸馏系统

> 本文档为 skill-anything 的最新设计方案，面向实施。设计演化过程和选型辩论见 [design-evolution.md](deprecated/design-evolution.md)。

---

## 一、项目定位

skill-anything 是一个**自主研究型 Agent 系统**——输入一个 GitHub repo URL，自闭环地完成知识理解、Skill 生成、评估验证、迭代优化，最终产出一套高质量的 Skill 文件。

**不是"提取管线"，而是"知识蒸馏"**——把 repo 中分散的、面向人类的知识，转化为 Agent 可以直接作为能力使用的格式。

### 核心命题

Agent 的能力上限 = 模型智能 × 可用上下文。

开发生态中存在大量使用门槛高、隐性知识多、且版本迭代快的框架和工具 repo。Skill 是将这些**面向人的知识**转化为**面向 Agent 的知识**的最佳载体。

> **Skill 是知识结晶，不是 API 手册。** Agent 读完 Skill 后应该"理解了这个领域"——能直接用内化的知识做出正确决策，而不只是知道怎么调 API。
> Skill 的价值 = f(知识差距 × Agent 遭遇频率 × 错误结构性)

Agent 表现不佳的根因不只是训练数据滞后，还包括：旧版模式在训练数据中占比过高（信噪比失衡）、隐性知识从未被文档化、以及跨系统组合的复杂性。Skill 要解决的是所有这些问题，而非仅做"版本补丁"。

### 适用 repo 类型

目标 repo 应该是：**Agent 在帮助开发者时频繁遇到、且当前表现不佳的 repo**。典型高价值场景：

1. **Agent 原生扩展机制**：MCP SDK——Claude Code 本身使用 MCP，Skill 化后形成自强化正循环
2. **训练数据滞后的主流框架**：Tailwind CSS v4、Next.js App Router、Pydantic v2
3. **AI 评估工具**：OpenJudge——与 skill-anything 的 Judge 模块存在战略协同
4. **隐性知识密集的工具库**：Anthropic SDK、instructor

---

## 二、架构

### 设计理念：Claude 即编排者

传统方案用外部代码（SDK 编程调度器）控制 Agent 的行为。skill-anything 采用相反的思路：

**Claude 自身就是 Orchestrator**——SKILL.md 提供工作流指引，Claude 自主判断当前阶段、调度子代理、读取产出物、做全局决策。外部代码仅提供辅助脚本（收敛计算、格式校验、viewer 生成）。

这种设计的优势：
- **灵活性**：Claude 可以根据具体情况灵活调整流程，而不是被管线推着走
- **零依赖**：不需要 Node.js/TypeScript 构建链，不需要 SDK 集成
- **可跨平台**：SKILL.md 同时适用于 Claude Code、Cursor、Cowork 等任何支持子代理的平台
- **简洁性**：复杂性在自然语言指令中，而非代码中

### 项目结构

```
skill-anything/
├── SKILL.md              # 主入口：Orchestrator 工作流指南
├── agents/               # 子代理角色指南
│   ├── researcher.md     # Researcher — 深度研究 repo、产出 Knowledge Map
│   ├── skill-writer.md   # Skill Writer — 生成/改进 Skill 文件
│   ├── eval-designer.md  # Eval Designer — 设计评估任务（与 Skill 隔离）
│   └── judge.md          # Judge — 盲法评判、多通道评分
├── scripts/              # 辅助脚本（机械操作）
│   ├── repo_manifest.py  # 扫描 repo 结构事实
│   ├── extract_api_surface.py  # 批量提取文件签名 + docstring
│   ├── find_related_issues.py  # 获取 GitHub issues 摘要
│   ├── blind_eval.py     # 盲化 eval 结果
│   ├── deblind_and_score.py  # 反盲化 + 评分 + 收敛
│   ├── convergence.py    # ε-δ 收敛判定 + results.tsv
│   ├── validate_skill.py # Skill 格式校验
│   ├── gen_viewer.py     # 生成 eval viewer HTML
│   ├── summarize_knowledge.py  # 从 Knowledge Map 生成域摘要
│   ├── register_skill.py     # 注册 Skill 到 registry
│   └── generate_catalog.py   # 从 registry 生成 catalog
├── references/           # SKILL.md 溢出内容 + 设计文档
│   ├── eval-loop.md      # 迭代循环详情（按需加载）
│   └── design-evolution.md
├── registry.json         # 已发布 Skill 的结构化目录
├── published/            # 已发布的 Skill 文件（由 register_skill.py 管理）
├── catalog-skill/        # 元 Skill：指向在线 catalog 的发现入口
│   └── SKILL.md
└── assets/
    └── viewer-template.html  # Eval Viewer HTML 模板
```

### 架构图

```
┌────────────────────────────────────────────────────────────┐
│  Claude（Orchestrator）                                     │
│  读取 SKILL.md → 按需加载 references/eval-loop.md            │
│  薄 Orchestrator：只调度和决策，机械操作交给脚本              │
│  状态：orchestrator-state.json（可中断恢复）                  │
└───────────────────────────┬────────────────────────────────┘
                            │  通过 Task tool spawn 子代理
         ┌──────────────────┼───────────────┐
         ▼                  ▼               ▼
  ┌──────────────┐   ┌────────────┐  ┌────────────────┐
  │ Researcher   │   │Skill Writer│  │ Eval Designer  │
  │ 入：profile  │   │ 入：KM +   │  │ 入：KM（无     │
  │  + repo      │   │  feedback  │  │  Skills！）    │
  │ 出：KM       │   │ 出：skills/│  │ 出：eval-tasks │
  └──────────────┘   └────────────┘  └────────────────┘
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                    ┌──────────┐      ┌──────────┐      ┌──────────┐
                    │Runner+S  │      │Runner-S  │      │Runner+S  │ ...
                    │(task 1)  │      │(task 1)  │      │(task 2)  │
                    └──────────┘      └──────────┘      └──────────┘
                          │                 │                 │
                          └─────────┬───────┘                 │
                                    ▼                         ▼
                             ┌──────────────────────────────────┐
                             │            Judge                 │
                             │ 入：Runner 结果对（盲化 A/B）      │
                             │ 出：scores + feedback + suggestion│
                             └──────────────────────────────────┘
                                           │
                                           ▼
                                  Claude 读取 Judge 反馈
                                  → 全局决策 → 下一轮迭代
```

### 设计原则

#### 原则一：上下文隔离与公平基线

- Runner+S 和 Runner-S **都有 repo/ 副本**——Eval 测量"蒸馏态 vs 原始态"，不是"有文档 vs 无文档"
- Runner+S 额外拥有 skills/，但不禁止查看 repo——Skill 的价值应体现为更高效的决策路径
- Eval Designer 和 Skills **必须隔离**——Eval Designer 在隔离临时目录工作，物理上看不到 skills/
- Judge 和 Runner 身份 **必须隔离**——Judge 收到盲化的 A/B 标签，不知道哪个用了 Skill

#### 原则二：自洽性检验

每个子代理在完成工作后，必须对自己的产出执行自洽性检验。检验逻辑嵌入在 `agents/*.md` 的角色指南中，是工作流程的内在组成部分。

#### 原则三：简洁性准则

> All else being equal, simpler is better. Removing something and getting equal or better results is a great outcome — that's a simplification win.

此准则同时适用于 Skill 内容和系统架构本身。

---

## 三、子代理角色

### Researcher（研究员）

- 输入：`repo-profile.yaml`（结构化事实，由 `repo_manifest.py` 生成）+ repo 源码
- 输出：语义域结构化的 Knowledge Map（YAML），知识点带蒸馏类型标注（cognitive/gotcha/pattern/api）
- 全权自治：自主决定域划分、研究策略、精读哪些文件。问题驱动，不是文件驱动
- 辅助脚本：`extract_api_surface.py`（批量签名提取）、`find_related_issues.py`（issues 摘要，可选）
- 详细指南：[agents/researcher.md](agents/researcher.md)

### Skill Writer（技能作者）

- 输入：Knowledge Map + 上一版 Skills + Judge 反馈
- 输出：新版 Skill 文件（SKILL.md + scripts/ + references/ + examples/）
- 遵循 Anthropic SKILL.md 标准
- 详细指南：[agents/skill-writer.md](agents/skill-writer.md)

### Eval Designer（评估设计师）

- 输入：**仅 Knowledge Map**（在隔离目录中工作，物理上不包含 skills/）
- 输出：评估任务集，包含三种类型：coding（含调试/陷阱检测）、reasoning、transfer（reasoning + transfer ≥ 40%）
- 详细指南：[agents/eval-designer.md](agents/eval-designer.md)

### Task Runner（任务执行器）

- `Runner+S`：repo/ 副本 + Skill 执行任务
- `Runner-S`：仅 repo/ 副本执行任务（基线）
- 每个 Runner 在独立的临时目录中运行，都能访问原始 repo

### Judge（评判官）

- 输入：Runner+S 和 Runner-S 的结果对（**盲化为 Output A/B**） + 评判标准
- 输出：盲法评分 + 获胜方 + 结构化反馈 + eval 质量反馈
- 详细指南：[agents/judge.md](agents/judge.md)

---

## 四、Knowledge Map 中间表示

知识结构 ≠ Skill 结构。Researcher 按**语义域**组织产出，携带足够的元数据让 Skill Writer 做出好的拆分决策，但不预先绑定 Skill 文件结构。

```yaml
domains:
  - id: "transport-selection"
    概念: "如何为 MCP server 选择合适的 transport 层"
    核心知识点:
      - "stdio transport 适用于本地进程间通信 [来源: README.md#L45-L52]"
      - "Streamable HTTP 是推荐的远程 transport [来源: docs/migration.md#L10]"
    隐性知识:
      - "stdio transport 的 stderr 不要用于业务日志 [来源: issues/234]"
    常见陷阱:
      - "混用 SSE 和 Streamable HTTP 的初始化代码导致连接失败 [来源: src/client.ts#L100]"
    关联域: ["tool-registration", "auth-sessions"]
    自足性: "高——理解此域不强依赖其他域"
    可脚本化要素:
      - "各 transport 的 boilerplate 初始化代码"
```

### 关键字段

- **自足性**：告诉 Skill Writer 哪些域可以独立成 Skill（高→独立，中→独立但声明依赖，低→合并到父域）
- **关联域**：Skill 间的依赖关系
- **可脚本化要素**：提示 scripts/ 目录应该放什么
- **隐性知识 + 常见陷阱**：Skill 最核心的价值

---

## 五、质量保障机制

### 多通道信号融合

| 信号通道 | 测量内容 | 噪声特征 |
|---|---|---|
| 执行通过率 | 代码能不能跑 | 低噪声，低区分度 |
| 断言覆盖率 | 输出结构是否符合预期 | 中噪声，中区分度 |
| LLM-as-judge 评分 | 语义质量 | 高噪声，高区分度 |
| 行为轨迹分析 | Agent 是否走弯路、回溯 | 低噪声，高区分度 |

**行为轨迹是被严重低估的信号**：如果 Agent 拿到 Skill 后仍然反复试错、grep 源码，说明 Skill 没有真正蒸馏到位。

### 综合评分公式

对 coding 类型 task：

```
composite = executionPass×0.1 + assertionCoverage×0.15 + (llmJudgeScore/5)×0.40 + trajectory×0.35
trajectory = max(0, 1 - toolCallsWith/toolCallsWithout)
```

对 reasoning / transfer 类型 task（无代码执行，纯推理/迁移产出）：

```
composite = (llmJudgeScore/5)×0.55 + trajectory×0.45
```

设计理由：两个 Runner 都有 repo 访问权，trajectory 差异真正反映蒸馏效率。llmJudgeScore 权重提升以覆盖语义质量、推理深度和知识内化程度。

### 收敛判定

```
s(t) = 综合评分
Δs = s(t) - s(t-1)

收敛条件：
- Δs < 0.03 连续 2 轮
- 或总迭代次数 > 10
- 或总预算耗尽（$30/repo）
- 且 s(t) > 0.6（质量底线）
```

**平台期重构**：连续 3 轮无改善时，让 Skill Writer 从头重构（而非微调）。

### Eval 质量自举

Eval set 不固定，在迭代中自动优化区分度：

- with/without 差不多 → 区分度不够，替换
- without 也能满分 → 太简单，替换
- with 也总是失败 → 任务不合理或超出 repo 范围，审查

### 知识溯源与幻觉防护

三层自检链防护：

1. **Researcher 自检**：每条知识点附上 repo 内来源，无法确认的标注 `confidence: "inferred"`
2. **Skill Writer 交叉验证**：对低置信度条目生成验证性代码放入 examples/，在 Runner 阶段实际执行
3. **Judge 行为反推**：Runner+S 按 Skill 指导但失败 → Judge 在 feedback 中标记 → 下轮迭代修正

---

## 六、Skill 产出物规范

### 完全对齐 Anthropic SKILL.md 标准

```
skill-name/
├── SKILL.md          # 必需：YAML frontmatter + Markdown 指令
├── scripts/          # 可选：可执行脚本
├── references/       # 可选：详细参考（溢出内容）
├── assets/           # 可选：模板、数据文件
└── examples/         # 可选：带预期输出的完整示例
```

**关键约束**：

| 字段 | 约束 |
|---|---|
| `name` | `[a-z0-9](-?[a-z0-9])*`，≤64 字符，与目录名一致 |
| `description` | 非空，≤1024 字符，包含 WHAT + WHEN |
| SKILL.md 正文 | < 500 行 |
| 文件引用 | 最多一层深度 |

**多 Skill 索引**：当产出 ≥ 2 个 Skill 时，必须在 `skills/index/SKILL.md` 生成索引入口（< 50 行），列出所有子 Skill 的名称、适用场景和路径。最终用户的 Agent 首先读取索引，按需加载具体 Skill。

### 自定义扩展

仅在 `metadata` 中用 `sa-` 前缀添加扩展键（非侵入式，标准兼容）：

```yaml
metadata:
  sa-source-repo: "repo-url"
  sa-source-commit: "commit-hash"
  sa-generated-at: "ISO-timestamp"
  sa-eval-score: "0.87"
  sa-iteration-count: "5"
```

### 工作空间目录

```
workspace/
├── .git/                         # git 版本控制
├── orchestrator-state.json       # Orchestrator 状态机（可恢复）
├── knowledge/
│   ├── repo-profile.yaml          # repo_manifest.py 产出（纯事实）
│   ├── api-surface.yaml          # extract_api_surface.py 产出（Researcher 按需调用）
│   ├── issues-summary.yaml       # find_related_issues.py 产出（Researcher 按需调用）
│   ├── knowledge-map.yaml        # Researcher 产出（合并后）
│   ├── domain-summary.yaml       # summarize_knowledge.py 产出
│   └── domains/                  # 域定向深研产出（大 repo 时）
├── skills/
│   ├── index/                    # 多 Skill 时的索引（可选）
│   │   └── SKILL.md
│   └── skill-name/
│       └── SKILL.md              # Skill Writer 产出
├── evals/
│   ├── eval-tasks.json           # Eval Designer 产出
│   └── results/
│       └── iter-N/
│           ├── eval-results.json
│           ├── blinded-eval-results.json  # blind_eval.py 产出
│           ├── blind-mapping.json         # blind_eval.py 产出（勿读）
│           ├── blind-judge-scores.json    # Judge 产出
│           ├── judge-scores.json          # deblind_and_score.py 产出
│           ├── iteration-summary.json     # deblind_and_score.py 产出（Orchestrator 读此文件）
│           └── viewer.html
└── results.tsv                   # 实验日志
```

---

## 七、辅助脚本

所有脚本用 Python 编写，仅依赖标准库（`find_related_issues.py` 需要 `gh` CLI）。

| 脚本 | 用途 | 调用方式 |
|---|---|---|
| `scripts/repo_manifest.py` | 扫描 repo 结构事实（纯事实，无语义判断） | `python scripts/repo_manifest.py <workspace> [--repo <path>]` |
| `scripts/extract_api_surface.py` | 批量提取文件签名 + docstring | `python scripts/extract_api_surface.py <files...> [--output <path>]` |
| `scripts/find_related_issues.py` | 获取 GitHub issues 摘要（可选） | `python scripts/find_related_issues.py <repo-path> [--keywords kw1,kw2] [--output <path>]` |
| `scripts/blind_eval.py` | 盲化 eval 结果（随机 A/B） | `python scripts/blind_eval.py <workspace> <iteration>` |
| `scripts/deblind_and_score.py` | 反盲化 + 评分 + 收敛检查 | `python scripts/deblind_and_score.py <workspace> <iteration> [--cost <usd>]` |
| `scripts/convergence.py` | ε-δ 收敛判定 + results.tsv 追加 | `python scripts/convergence.py <workspace> <score> [--cost <usd>] [--status keep\|discard] [--desc "text"]` |
| `scripts/validate_skill.py` | 验证 Skill 目录格式合规性 | `python scripts/validate_skill.py <skill-dir>` |
| `scripts/gen_viewer.py` | 从 workspace 数据生成 eval viewer HTML | `python scripts/gen_viewer.py <workspace> [iteration]` |
| `scripts/summarize_knowledge.py` | 从 Knowledge Map 生成域摘要 | `python scripts/summarize_knowledge.py <workspace>` |
| `scripts/register_skill.py` | 注册 Skill 到 registry | `python scripts/register_skill.py <workspace> <repo-url> [--name name]` |
| `scripts/generate_catalog.py` | 从 registry 生成 catalog | `python scripts/generate_catalog.py` |

版本控制用 workspace 内的 git：

```bash
# 快照
cd workspace && git add -A && git commit -m "iter-N: description" && git tag skill-v<N>

# 回滚
cd workspace && git checkout skill-v<N-1> -- skills/
```

---

## 八、Skill Catalog（外部化发布）

蒸馏完成后，可将 Skill 发布到 catalog 供跨会话复用。

### 架构

```
registry.json          ← 结构化数据源（single source of truth）
       │
       ▼
generate_catalog.py    → catalog-skill/SKILL.md  (repo 内)
                       → docs/catalog/SKILL.txt  (GitHub Pages)
       ▲
       │
register_skill.py      ← 从 workspace 注册新 Skill
```

### 三级发现机制

| 层级 | 文件 | 大小 | 作用 |
|------|------|------|------|
| L0 指针 | `catalog-skill/SKILL.md` | ~40 行 | 只含 URL，指向在线目录 |
| L1 目录 | GitHub Pages SKILL.txt | ~100 行 | 所有已发布 Skill 的分类表 |
| L2 详情 | `published/<name>/SKILL.md` | 各异 | 具体 Skill 的完整内容 |

Agent 按需逐级深入：L0（40 行）→ L1（100 行）→ 仅加载目标 Skill。

### 发布流程

```bash
python scripts/register_skill.py workspace/ https://github.com/org/repo
python scripts/generate_catalog.py
git add published/ registry.json catalog-skill/ docs/
git commit -m "publish: repo-name skills"
git push  # GitHub Pages 自动部署
```

---

## 九、MVP 选型与实施路线图

### Phase 0 冒烟 repo A：Tailwind CSS v4

| 维度 | 评估 |
|---|---|
| 为什么选它 | 知识点集中在配置方式变更（v3→v4），蒸馏难度最低 |
| eval 优势 | 二元化：`npm run build` 成功或失败 |
| 蒸馏价值 | 模型训练截止日之后的重大 API 变更 |

### Phase 1 主 MVP repo：MCP SDK

| 维度 | 评估 |
|---|---|
| 为什么是主 MVP | Claude Code 原生使用 MCP → 自强化正循环 |
| 隐性知识密度 | 9.5/10 |
| eval 设计 | ① 构建文件系统 MCP server ② REST API 适配器 ③ 带认证的 MCP server |

### 成功标准

```
- with_skill 综合评分 > without_skill 至少 20%
- with_skill 工具调用次数 < without_skill 至少 30%
- with_skill 不出现"读了 Skill 又去翻源码"的行为
- 至少 1 轮迭代后评分有显著提升（验证反馈环有效）
```

---

## 十、关键决策汇总

| 决策项 | 结论 | 核心理由 |
|---|---|---|
| **架构模式** | **Claude 即编排者 + 辅助脚本** | Claude 自主调度，无需 SDK 构建链，跨平台适用 |
| **子代理调度** | **原生 Task tool + agents/*.md** | 一致的角色定义，平台无关 |
| **状态管理** | **文件系统即状态** | 零依赖，Claude 通过文件存在性判断进度 |
| Eval Designer 隔离 | 架构隔离（隔离临时目录） | 从协议纪律升级为物理保证 |
| Judge 盲法评判 | Claude 手动盲化 A/B + 反盲化 | 消除 Judge 对 Skill 输出的偏向 |
| Skill 格式 | 完全对齐 Anthropic SKILL.md 标准 | 27+ 平台兼容，事实标准 |
| 版本控制 | workspace 内 git snapshot/rollback | 零依赖，原生版本控制 |
| 辅助脚本 | Python 标准库 | 零外部依赖，跨平台 |
| 质量保障 | 自洽性检验（agents/*.md 内置）+ 多通道评分 | Agent 自检 + 架构隔离 |
| 成本控制 | 全局上限 $30/repo | 防止失控，留足迭代空间 |
