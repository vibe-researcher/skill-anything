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
  sa-generated-at: "ISO-timestamp"
---

# Skill 标题

[核心指令，< 500 行，组织结构由内容决定]

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

### 多 Skill 产出时的索引

当产出 2 个或以上 Skill 时，在 `skills/index/SKILL.md` 生成索引入口（< 50 行）。单 Skill 不需要索引。

## 拆分决策

基于研究报告中各主题的自足性判断：能独立理解的主题可独立成 Skill，高度耦合的合并。多 Skill 时在 `metadata` 中用 `sa-depends-on` 声明依赖。

## 写作哲学

- **用理由替代强制**：如果想写 `ALWAYS` 或 `NEVER`，重构为解释性表述——让 agent 理解为什么
- **聚焦指令性内容**：agent 应该做什么，不是百科式描述
- **迭代时泛化**：Grader 反馈说"任务 X 做错了"→ 修复应是泛化的指导，不是针对 X 的补丁
- **简洁性准则**：删除后效果不降 = 应该删

## 自洽性检验

```
硬性：
  ✓ name 与目录名一致，符合格式
  ✓ description 非空，≤1024 字符，包含 WHAT + WHEN
  ✓ SKILL.md 正文 < 500 行
  ✓ 所有引用的文件实际存在

自问：
  △ 读完 Skill 后不需要再读 repo 就能做出 80% 正确的领域决策？
  △ 删除任意一段后如果不影响理解 = 应该删？
  △ 迭代修改是泛化的指导，还是针对特定 eval 任务的补丁？
```
