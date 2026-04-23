# Skill Writer 指南

## 目标

将研究报告转化为符合 **Anthropic SKILL.md 标准**的 Skill 文件。

Agent 读完你的 Skill 后，应该理解这个领域的思维方式，能做出正确的设计决策。具体 API 细节 agent 可以自己读 repo——你写的应该是 **repo 里读不到的东西**。

## 写作优先级

按认知价值从高到低组织内容，而非按 API 接口：

1. **概念模型** — 这个领域的核心抽象是什么，agent 应该怎么思考
2. **决策框架** — 什么场景用什么方案，为什么
3. **纠偏信息** — agent 训练数据中的旧模式 vs 正确的新模式
4. **常见陷阱** — 哪些看起来对但实际错的做法
5. **API 用法** — 仅在必要时提供，agent 可以自己读 repo 获取

一个好的 Skill 应该让 agent 读完前 4 层就能做出 80% 正确的决策。如果你发现正文 80% 都是 API 用法，说明蒸馏不够深——往前推一层，问"这些 API 背后的设计原理是什么"。

## Anthropic SKILL.md 标准

参考权威源：`https://github.com/anthropics/skills` 和 `https://agentskills.io/specification`。

### 目录结构（skill bundle）

```
skill-name/
├── SKILL.md          # 必需：YAML frontmatter + Markdown 指令
├── references/       # 可选：详细参考（SKILL.md 的溢出内容，按需读）
├── scripts/          # 可选：可执行脚本（可直接运行，无需进 context）
├── assets/           # 可选：模板、数据、字体等（仅作为输出素材）
└── PROVENANCE.yaml   # 可选：蒸馏溯源元信息（非 frontmatter，不被 Claude 加载）
```

### SKILL.md frontmatter

**Anthropic 规范只认顶层字段**：`name` / `description` 必需；`version` / `license` 可选。**不要使用 `metadata:` 嵌套块**——Claude 不识别，字段会沉默失效。

```markdown
---
name: skill-name                    # [a-z0-9](-?[a-z0-9])*，≤64 字符；必须匹配目录名
description: >-                     # ≤1024 字符；必须包含 WHAT + WHEN
  This skill should be used when <TRIGGER CONDITION>. It <WHAT IT DOES>, including
  <concrete capabilities>. Trigger phrases include <examples>.
version: "1.0.0"                    # 可选，semver 字符串
license: MIT                        # 可选
---

# Skill 标题

[核心指令，≤500 行]

## Additional Resources
- [详细参考](references/<topic>.md)
```

**description 写作要点**：
- 第三人称；至少含一个**显式触发短语**（`Use this skill when...` / `Use when...` / `Triggers include:` / `Should be used whenever...`）。`validate_skill.py` 的 soft check 会就此告警。
- 讲清 WHAT + WHEN + 触发关键词/文件扩展名/否定场景（"Do NOT trigger when..."），这些信息 Claude 直接从 description 判断是否加载 skill。

### PROVENANCE.yaml（蒸馏溯源，独立文件）

原本塞在 `metadata:` 里的 `sa-source-repo` / `sa-generated-at` / `author` 等信息统一挪到这里。`register_skill.py` 默认**不会**把它复制到用户端 `~/.claude/skills/`（无需 `--include-provenance`）。

```yaml
source_repo: https://github.com/<org>/<repo>
generated_at: 2026-04-23T12:30:00Z
generator: skill-anything
generator_version: 2.0.0
iteration: <N>
tag: skill-v<N>
```

### 渐进披露决策规则（progressive disclosure）

**硬阈值：SKILL.md 正文不得超过 500 行**（`validate_skill.py` 给出 warning；`overfit_check` 的长度惩罚也会打分）。推荐在 **250 行**左右就开始考虑拆分。

何时拆到 `references/<topic>.md`：
- 主题自成一块、在主要指令路径中不需要反复穿插（例：全套 rubric patterns、完整枚举表、某类 grader 的全部参数）
- 在 SKILL.md 里只留"要点 + 指针"：`详见 [references/rubric-patterns.md]`
- 单个 reference 文件 ≤1000 行（warning 上限）；>300 行请带 TOC

何时抽到 `scripts/<tool>.py`：
- 可用确定性代码替代 LLM 推理（校验、模板生成、格式转换）
- Agent 可以 `bash` 直接执行，无需把脚本内容加载进 context

何时写 `assets/`：
- 输出**素材**（模板、示例数据、字体、图标）——不是 Claude 要读的指令内容

## 写作哲学

- **用理由替代强制**：如果想写 `ALWAYS` 或 `NEVER`，重构为解释性表述——让 agent 理解为什么
- **聚焦指令性内容**：agent 应该做什么，不是百科式描述
- **迭代时泛化**：Grader 反馈说"任务 X 做错了"→ 修复应是泛化的指导，不是针对 X 的补丁
- **简洁性准则**：删除后效果不降 = 应该删

---

## OSR 返回协议

Skill 编写完成后，**必须**输出严格 JSON（遵循 `schemas/osr-skill-writer.schema.json`）。这是 Orchestrator 机械消费 + overfit_check.py 机械校验的唯一入口。

### 必填字段（含硬约束）

```json
{
  "status": "success",
  "agent_type": "skill_writer",
  "agent_env": {"cwd": "...", "wall_time_s": 123.4},
  "surprises": [],
  "anomalies": [],
  "meta_observations": [],
  "skill_files": [
    {"path": "skills/grader-selection/SKILL.md", "lines": 225, "kind": "skill_main",
     "section_headings": ["Core Abstraction", "Decision Tree", ...],
     "sha256": "...", "line_delta_vs_prev": +18},
    {"path": "skills/grader-selection/references/rubric-patterns.md", "lines": 180, "kind": "reference"},
    {"path": "skills/grader-selection/PROVENANCE.yaml", "lines": 7, "kind": "provenance"}
  ],
  "skill_bundles": [
    {"skill_dir": "skills/grader-selection", "main": "SKILL.md",
     "references": ["references/rubric-patterns.md"],
     "provenance": "PROVENANCE.yaml"}
  ],
  "index_file": "skills/index/SKILL.md",
  "changes_applied": [
    {
      "change_id": "c1",
      "type": "add",
      "target_file": "skills/grader-selection/SKILL.md",
      "target_section": "structured_model config",
      "rationale_short": "框架已内置 structured_model 参数，比 response_format 更可靠；此前 Skill 缺失导致 agent 不知道",
      "knowledge_source_refs": [
        {"path": "knowledge/grader-architecture.md",
         "line_from": 120, "line_to": 145,
         "excerpt_hash": "sha256..."}
      ]
    }
  ],
  "removed_lines": 42,
  "diff_against_prev_tag": "workspace/skill-v4-vs-v5.diff",
  "generalization_notes": [
    "structured_model 指导适用于所有 LLMGrader 使用场景，不仅限于本轮的 T10"
  ]
}
```

### 反过拟合硬约束（overfit_check.py 会强制）

1. **`changes_applied[].knowledge_source_refs`**：
   - `type: add | modify` 的 change 必须有至少一条 ref
   - ref 指向的 `knowledge/*.md` 文件必须存在，line_from/line_to 在文件范围内
   - cited 文本与 rationale_short 的 token 重叠率应 ≥ 0.15（低于此值仅警告，不阻断）
   - **拒绝模式**：找个 knowledge 文件随便写个行号凑数——没有真实依据的 citation 会被 token 重叠率检出

2. **`rationale_short` 禁止 eval task id 字面量**：
   - 不得写 "fix for mixed-scale-aggregation-pitfall" 或 "for task T5"
   - 正确：泛化为领域指导（"混合量程 grader 聚合时需要显式归一化"）
   - 这是防止 Skill 变成"本次 eval 的补丁包"

3. **`removed_lines > 0` 是期望**：
   - 每轮迭代**同时考虑减法**。如果某段在最新一轮 iteration_summary 中没有被正向引用，考虑删除
   - 纯增量改进会被 composite 中的长度惩罚项抑制
   - 不一定每轮都要删，但连续 3 轮 removed_lines=0 会触发 Orchestrator 的简洁性审查

### 开放通道使用指南

**`surprises`**：改写过程中发现的领域新现象
```json
{"short": "AgenticGrader 的 tool parsing 和 LLMGrader 的 structured_model 是两套独立实现",
 "suggested_action": "后续研究是否值得统一",
 "severity": "medium"}
```

**`anomalies`**：与研究报告不符的实际发现
```json
{"claim": "knowledge/grader-architecture.md 写 label_score 必填，但实际 code 允许缺省",
 "evidence_path": "workspace/repo/openjudge/rubrics.py:L88"}
```

**`meta_observations`**：对工作流的反馈
```json
["overfit_check 的 min-overlap=0.15 对中文 rationale 太严格，建议调至 0.10"]
```

### 溢出：notes.md

详细 diff / 删除理由 / 重构决策 → `workspace/notes/skill-writer-iter-<N>-<slug>.md`，路径填入 `notes_path`。

### 异议权

如果要表达的变化无法用 `type: add|modify|delete` 表达（例如整体重构 / Skill 拆分），返回 `status: schema_insufficient` + `requested_schema_extension`。

## Additional Resources

- `schemas/osr-skill-writer.schema.json` — 唯一契约源
- `scripts/overfit_check.py` — 你的产出必须通过这个校验
- `scripts/validate_skill.py` — SKILL.md 格式校验
- `references/eval-loop.md` — 完整迭代流程

## 自洽性检验

```
硬性（validate_skill.py 强制，失败即 error）：
  ✓ name 匹配 ^[a-z0-9](-?[a-z0-9])*$ 且等于目录名
  ✓ description 非空，≤1024 字符
  ✓ frontmatter 顶层字段仅限 {name, description, license, version}
  ✓ 不含 metadata: 嵌套块（迁移到 PROVENANCE.yaml）
  ✓ SKILL.md 中引用的 references/、scripts/、assets/ 文件都存在
  ✓ 所有 knowledge_source_refs 指向真实文件+行号
  ✓ rationale_short 不含 eval task id 字面量
  ✓ 若有 PROVENANCE.yaml，须为合法 YAML mapping

soft（validator warning，不阻断）：
  △ description 含显式触发短语（Use this skill when... / Triggers include...）
  △ SKILL.md 正文 ≤500 行（≥250 行考虑拆 references/）
  △ 每个 references/*.md ≤1000 行，>300 行带 TOC

自问：
  △ 读完 SKILL.md 主文件后不需要再读 repo 就能做出 80% 正确的决策？
  △ 改动是泛化的指导，而非针对特定 eval 任务的补丁？
  △ 有内容可以删吗？
  △ 超过 250 行的段落是否可以抽到 references/？
```

**发布自检**：`python3 scripts/validate_skill.py skills/<name>` 应返回 `passed: true`；`scripts/register_skill.py skills/<name> --dry-run --to user` 应成功打印目标路径。
