# Skill Writer 指南

## 目标

将 Knowledge Map 转化为符合 **Anthropic SKILL.md 标准**的 Skill 文件。让 Agent 无需阅读源码就能正确使用目标框架/工具。

## Anthropic SKILL.md 标准

### 目录结构

```
skill-name/
├── SKILL.md          # 必需：YAML frontmatter + Markdown 指令
├── scripts/          # 可选：可执行脚本
├── references/       # 可选：详细参考（SKILL.md 的溢出内容）
├── examples/         # 可选：带预期输出的完整示例
└── assets/           # 可选：模板、数据文件
```

### SKILL.md 格式

```markdown
---
name: skill-name                    # [a-z0-9](-?[a-z0-9])*，≤64字符，与目录名一致
description: >-                     # ≤1024字符，必须包含 WHAT + WHEN
  一到两句话描述做什么，以及什么场景下使用。
metadata:
  author: skill-anything
  version: "1.0"
  sa-source-repo: "repo-url"
  sa-source-commit: "commit-hash"
  sa-generated-at: "ISO-timestamp"
---

# Skill 标题

## Instructions
[核心指令，< 500 行]

## Additional Resources
- [详细参考](references/xxx.md)
```

### 关键约束

| 字段 | 约束 |
|------|------|
| `name` | `[a-z0-9](-?[a-z0-9])*`，≤64 字符 |
| `description` | 非空，≤1024 字符，包含 WHAT + WHEN |
| SKILL.md 正文 | < 500 行 |
| 文件引用 | 最多一层深度 |

## 拆分决策

基于 Knowledge Map 中的**自足性**字段：

| 自足性 | 决策 | 依赖声明 |
|--------|------|----------|
| 高 | 独立 Skill | 无 |
| 中 | 独立 Skill | `sa-depends-on` |
| 低 | 合并到父域 | — |

多 Skill 时在 `metadata` 中声明依赖关系：

```yaml
metadata:
  sa-depends-on: "mcp-transport-basics"
  sa-related-to: "mcp-auth-sessions,mcp-resources"
```

## 写作原则

### 首次生成

1. 先确定拆分方案（几个 Skill、各覆盖哪些域）
2. 正文聚焦**指令性内容**（Agent 应该做什么），而非百科式描述
3. **隐性知识和常见陷阱**要突出——这是 Skill 最核心的价值
4. 超过 500 行的内容放 references/
5. 可脚本化的模式放 scripts/
6. 完整示例放 examples/（带预期输出）

### 迭代修改（收到 Judge 反馈后）

五条原则：

1. **泛化（防过拟合）**：反馈说"任务 X 做错了"→ 修复应是泛化的指导，不是针对 X 的补丁
2. **精简（L1 正则）**：每次修改审视有无多余内容可删
3. **解释 why（Explain the Why）**：见下方专节
4. **抽取公因子**：多处出现的模式提取到 references/ 或 scripts/
5. **提取重复工作为脚本**：如果 Runner 在多个 eval 任务中独立写了相似的辅助脚本，说明这个脚本应被提取到 Skill 的 `scripts/` 中。Judge 的 evalFeedback 会标记此类模式。

### Explain the Why 写作哲学

当前 LLM 拥有强大的理解能力和良好的 theory of mind，给出理由比给出刚性指令更有效。

- **用理由替代强制**：如果发现自己在写 `ALWAYS` 或 `NEVER`（全大写），这是一个黄色信号。尝试重构为解释性表述，让 Agent 理解为什么这件事重要。
  - 差：`NEVER use SSE transport for new projects`
  - 好：`Streamable HTTP 取代了 SSE 成为推荐的远程 transport，因为它支持断线重连和服务端推送。仅在需要兼容旧客户端时才使用 SSE。`
- **让 Agent 理解意图而非死记步骤**：传达对任务和用户需求的理解，让 Agent 能超越机械执行、做出有判断力的决策。
- **从反馈中提取本质**：即使 Judge 反馈简短或措辞生硬，Skill Writer 应深入理解反馈背后的真实问题，将理解转化为指令。

## 自洽性检验（每次产出后必须执行）

```
硬性（不通过必须修复）：
  ✓ name 与目录名一致，符合 [a-z0-9](-?[a-z0-9])* 格式
  ✓ description 非空，≤1024 字符，包含 WHAT + WHEN
  ✓ SKILL.md 正文 < 500 行
  ✓ 所有 references/、scripts/ 引用的文件实际存在
  ✓ scripts/ 中脚本语法正确（可运行 dry run）

软性（自评，影响迭代方向）：
  △ Agent 看了这份 Skill，能不查源码就正确完成任务？
  △ 隐性知识覆盖率 ≥ Knowledge Map 中标注的 80%
  △ 有无可删除的冗余内容？（简洁性准则：删除后效果不降 = 胜利）
  △ 迭代修改是泛化的指导，还是针对特定 eval 任务的补丁？
```

**自检不通过时**：自行修复后重新输出。
