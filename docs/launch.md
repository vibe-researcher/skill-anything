# Launch Notes (internal)

> This is a **private launch-prep doc**, not a user-facing page. Review before posting.
> Kept in-repo so the tweet/HN drafts stay versioned with the code.

---

## Positioning (the one thing to drill)

> **Agent-Skill Compiler**. Point it at a GitHub repo; get back a bundle of `SKILL.md` files your agent loads like it's already an expert. Every bundle ships with a blind-eval audit trail.

Three substitute words for "skill" if audience glazes over:
- **compiled domain knowledge**
- **pre-learned capability**
- **loadable expertise**

Do not say: "autonomous system", "agentic framework", "AI-powered pipeline". These are generic and invite pattern-match rejection.

---

## The core tweet (launch day, single post)

> I built an agent that distills GitHub repos into skills other agents can load.
>
> First run: the agent cheated its own eval — wrote its own runner's outputs, played the grader's role, faked tool counts.
>
> Here's the 16-problem postmortem, the rebuild, and the first converged result at 0.92.
>
> [repo link]

**Why this works:**
- Conflict ("cheated") → hook
- Number (0.92) → credibility
- Postmortem + rebuild → signals engineering maturity
- No adjectives, no "revolutionary" — just what happened

**Do NOT open with:** "Excited to share...", "We're proud to announce...", "After months of work..."

---

## HN title candidates (ranked)

1. **Show HN: skill-anything – our first agent cheated its own eval, so we rebuilt it**
2. Show HN: An agent-skill compiler with a public blind-eval audit trail
3. Show HN: Distill any GitHub repo into agent-loadable skills (with receipts)

#1 wins because it frontloads the conflict. HN moderators sometimes trim "Show HN:" — make sure the hook survives.

## Show HN body (copy verbatim, ≤280 words)

> skill-anything is a tool that takes a GitHub repo URL and produces a set of `SKILL.md` files — the Anthropic agent-skill standard — that Claude Code, Cursor, or any compatible agent can load as if it had already read the repo.
>
> I built a v1 that looked great: clean autonomous loop, blind A/B evaluation, composite score of 0.86 at convergence. Then I stopped to audit the transcripts. The top-level orchestrator had been writing its own runners' outputs, playing the grader's role, and hand-picking tool-call counts. "Blind evaluation" was a directory-naming convention.
>
> The 16-problem critique is in the repo ([`critique-report.json`](link)). The rebuild (v2) enforces physical worktree isolation on every sub-agent — the grader architecturally cannot see the blind-mapping file. OSR structured returns replace natural-language parsing. Nine guardrails halt the loop on suspicious patterns.
>
> First v2 run (on OpenJudge, an LLM-as-judge framework) converged at 0.92 composite (iter-2), won 12/12 tasks on quality, regressed on iter-3 to 0.78 which I published as `status: beta`. The full audit trail — per-iteration grader scores, blind-mapping, SHA-256 hashes — ships in `published/openjudge-receipts/`.
>
> The opinionated claim: **agent-graded-by-agent is becoming the default, and silent failure in that setup looks exactly like success.** The only honest response I know is to publish the receipts.
>
> Everything is Apache 2.0. Python stdlib only. Works with Claude Code out of the box.

## Expected FAQ (have answers ready)

**Q: How much does one distillation cost?**
> Honestly: not instrumented yet. One OpenJudge run ate several million tokens across Researcher / Skill Writer / Eval Designer / Runners / Graders. Cost telemetry is roadmap-blocked on — this is the #1 thing I want to ship next. For now, run it on a throwaway target first.

**Q: Is this just a prompt generator?**
> No. A prompt generator produces text. This produces text **plus an audit trail**: grader rationales, blind mapping, score history, per-iteration regressions. The difference is falsifiability.

**Q: Why is the K=1 grader run "beta"?**
> Because K=1 graders can share the model family's systematic biases and miss them. K=3 ensemble is the recommended default. I shipped K=1 because cost; the README calls this out honestly.

**Q: What about Anthropic's own Skill market?**
> Orthogonal. Theirs is curated-by-humans. Mine is automated-from-repos with receipts. If Anthropic wants to cite the eval manifest format as a standard, that's a win.

**Q: Why not build this as a framework?**
> The "framework" is 27 stdlib-Python scripts and a markdown file the orchestrator reads. That's the feature, not a limitation. Every line of build chain is a barrier to adoption.

**Q: Can it distill my repo?**
> Open [a distillation-request issue](link). First-come is not how targets get picked — value + novelty + feasibility. The OpenJudge case was chosen because it has high-impact silent failures (grader scale mismatches) that directly hurt agents in production.

**Q: Why the failure-story angle? Isn't that bad marketing?**
> Agents will be writing each other's evaluations soon. The failure modes I tripped on are everyone's failure modes. I'd rather put the receipt in the open than ship a clean story.

---

## Channels & sequencing

1. **Day -1**: local commit everything, verify links, run `register_skill.py --dry-run` from a fresh checkout.
2. **Day 0, 6-8 AM PT**: push to GitHub, enable GitHub Pages if we want the catalog URL live (otherwise strip that reference from README first).
3. **Day 0, 9 AM PT**: post core tweet. Thread with 3 follow-ups (each with a screenshot or snippet — gallery table, critique-report excerpt, eval-manifest snippet).
4. **Day 0, 10 AM PT**: Show HN post. Do not repost if it doesn't hit the front page — one shot per project.
5. **Day 0, afternoon**: reply to every HN comment within 2 hours during first 6 hours. Even dismissive ones. Treat top-level critiques as collaboration invitations.
6. **Day 0, evening**: cross-post to r/LocalLLaMA and r/ClaudeAI. Different angle: "blind-eval audit trail for agent skills — feedback wanted".
7. **Day +1**: Anthropic Discord #skills channel (soft-post, not launch).
8. **Day +7**: retrospective — what did critics challenge, what landed, what needs the roadmap moved up.

## Pre-launch checklist

- [ ] Enable GitHub Pages OR remove `catalog_url` references from README/registry.
- [ ] Repo visibility = public.
- [ ] Verify every README link resolves (use `scripts/` one-liner or manual click).
- [ ] Run `register_skill.py ... --dry-run` from a clean clone to confirm the "Try this now" command works verbatim.
- [ ] `validate_skill.py published/openjudge-{index,grader-selection,rubric-workflow,agent-eval}` all pass.
- [ ] `critique-report.json` loads valid JSON.
- [ ] No unresolved TODO / FIXME in README.
- [ ] Tag the release: `v0.2.0-beta`.

## What NOT to do

- **Don't claim "converged 0.86"** anywhere. The real number is iter-2 0.92 kept, iter-3 0.78 regressed. Every honest reference should include both.
- **Don't describe this as "autonomous"** without qualification. Claude in the loop is not fully autonomous; it's Claude with architectural constraints. Misstating this undermines the rest of the pitch.
- **Don't ship the badge**. Ecosystem badges before 3+ distilled targets look like cosplay.
- **Don't argue with pedants on thread**. State the claim, link the receipt, move on. Let the manifest do the arguing.
- **Don't start a Discord/Slack**. Not yet. Issues + threads for at least 30 days.

---

*Committed intentionally — if anyone else launches a fork, this playbook is the head-start. Update before every launch attempt.*
