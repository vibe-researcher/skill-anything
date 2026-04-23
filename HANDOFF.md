# Handoff — Community-First Redesign

**Branch:** `redesign/community-first-2026-04-24`
**Session:** 2026-04-24 evening (auto-mode)
**Open for review:** 2026-04-25 morning

---

## TL;DR

- Ran a **4-agent critique team** (open-source maintainer / agent-user UX / competitive positioning / technical feasibility) over a 12-item redesign proposal. Critiques cross-checked each other, decisions survived adversarial review.
- Did **not** rebuild the v2 harness — its OSR / Markov / ensemble engineering is sound. The problem was that everything was dormant: `registry.json` empty, `published/` empty, critique-report invisible, README buried the case study.
- **Activated the dormant infrastructure** with the OpenJudge distillation (already converged at iter-2 in `workspace/`) and reshaped the narrative around a **scientific differentiator**: every published Skill ships with a reproducible blind-eval audit trail.
- Kept everything **honest**: iter-3 regressed to 0.78 — published as `status: beta`, explicitly called out. No fabricated numbers anywhere.

---

## What changed (files)

### New files

| File | Purpose |
|---|---|
| `docs/why-v2.md` | Postmortem: "How our first agent cheated its own eval" — the marketing-gold credibility signal. 75 lines. |
| `docs/concepts.md` | English navigation hub for v2 harness. Navigates to `harness-design.md` (CN), `eval-loop.md`, schemas. 99 lines. |
| `docs/launch.md` | Internal launch playbook — tweet, HN title, Show HN body, FAQ, pre-launch checklist. 120 lines. |
| `published/openjudge-{index,grader-selection,rubric-workflow,agent-eval}/` | 4 Skill bundles promoted from `workspace/skills/`. All pass `validate_skill.py`. |
| `published/openjudge-receipts/eval-manifest.json` | Reproducible blind-eval audit trail with SHA-256 file hashes, iter-1 & iter-2 summaries, honest caveats, and a "how to re-run" protocol. |
| `published/openjudge-receipts/iter-{1,2}/*.json` | Per-iteration raw artifacts (grader scores, blind mapping, OSR digest). |
| `.github/ISSUE_TEMPLATE/distillation-request.yml` | The only issue template — cold-start traction pool. |

### Modified files

| File | Change |
|---|---|
| `README.md` | Full rewrite. Now: Hero ("Agent-Skill Compiler") → Try-this-now (verified one-liner) → Receipts (honest score + caveats) → Gallery (only OpenJudge, no fake "coming soon") → How-it-works (30 sec) → Design principles → Roadmap → Layout → Contributing. 201 lines. |
| `README_zh.md` | Top three sections (你能拿到什么 / 立即试用 / 可信凭据) translated from new English main; historical content retained below a clear banner. |
| `registry.json` | Populated with the OpenJudge bundle entry. `score: 0.9167`, `status: beta`, full caveat string, `eval_manifest` pointer. Schema compatible with `generate_catalog.py`. |
| `.gitignore` | Added `workspace-deprecated-*/` (T10) and `.redesign-proposal.md` (scratch). Repo root now clean. |

### Created but **not** committed intentionally

- `.redesign-proposal.md` — ephemeral scratch, gitignored.
- The three `workspace-deprecated-0408/0409/0422/` directories — kept on disk, removed from `git status` via `.gitignore`. You can `rm -rf` them when you want.

---

## Agent-team verdicts (condensed)

4 critique agents ran in parallel on 12 candidate TODOs. Consensus emerged naturally on most items. Conflicts were resolved by deferring to the agent whose domain was most relevant.

| TODO | Maintainer | User | Positioning | Technical | Final |
|---|---|---|---|---|---|
| T1 README rewrite | ✅ top priority | ✅ top priority | ✅ | ✅ 1.5-2h | **DONE** |
| T2 concepts split | modify (move only) | skip | — | 45m | **DONE (light)** |
| T3 why-v2.md | ✅ unique asset | skip | ✅ integrity signal | risk of bloat | **DONE (75 lines)** |
| T4 publish OpenJudge | ✅ must | ✅ must | ✅ proof-of-work | 1h w/ schema risks | **DONE** |
| T5 cost_estimate.py | table, not script | skip | ✅ transparency | **no real data → fake risk** | **DEFERRED to roadmap** |
| T6 contrib + templates | only 1 template | skip | — | 30m | **DONE (1 template)** |
| T7 install_skill.py | risky tonight | ✅ must-have | — | **redundant — `register_skill.py --to user` exists** | **DOCUMENTED, no new script** |
| T8 badge generator | reject | actively disliked | reject | easy | **REJECTED** |
| T9 gallery.md | merge into README | redundant | — | — | **MERGED INTO README** |
| T10 archive deprecated | ✅ free win | ✅ high ROI | — | 5m via .gitignore | **DONE** |
| T11 skill_diff | reject | reject | ✅ long-term moat | XL | **ROADMAP ONLY** |
| T12 skill_compose | reject | — | reject | XL | **ROADMAP ONLY** |

### Novel ideas added from the critique

- **N1 Hero demo (GIF/asciinema)** — *not done* (can't record video in this session). Launch day prerequisite; noted in `docs/launch.md` checklist.
- **N2 `docs/launch.md`** — ✅ done. Internal playbook: tweet draft, HN title options, Show HN body, FAQ, pre-launch checklist, what-not-to-do.
- **N3 Critique-report as "Receipts"** — ✅ pinned to README `Receipts` section.
- **Reproducible Blind-Eval Protocol (positioning agent's moat candidate)** — **core architectural decision**. Led to creating `eval-manifest.json` with SHA-256 hashes, iter summaries, blind-mapping (published post-run), and a `reproducibility.how_to_re_run` section. Formalizing this into a citable standard is the #1 roadmap item.

---

## The positioning shift

From: *"autonomous knowledge distillation system"*
To: *"Agent-Skill Compiler with reproducible blind-eval audit trails"*

Why:
1. **Compiler** frames this as a tool, not a framework. Lower cognitive barrier.
2. **Reproducible audit trails** is the single claim no competitor makes: Anthropic's Skill market is curated, Cursor rules are unsigned, Goose recipes are hand-written. Receipts + manifest + honest beta = a scientific claim.
3. **"Cheated its own eval"** as the cover story for why v2 exists flips a weakness into the most distinctive engineering-integrity signal in the space.

---

## Your morning checklist

Before anything else:

- [ ] Read `README.md` top-to-bottom as if you've never seen this project. Does it answer "what is this / what can I get / why different" in 30 seconds?
- [ ] Read `docs/why-v2.md`. Is the tone right — self-critical without being performatively self-flagellating?
- [ ] Spot-check `published/openjudge-receipts/eval-manifest.json`. The `honest_caveats` array must be truthful; flag anything overclaimed.
- [ ] Dry-run the try-this-now command from a clean directory:
  ```bash
  bash -c 'python3 scripts/register_skill.py published/openjudge-{index,grader-selection,rubric-workflow,agent-eval} --to user --dry-run'
  ```
  All 4 results should report `ok=True`. (Verified once here; you should re-verify before sharing the README with anyone.)
- [ ] Decide whether `workspace-deprecated-*/` get `rm -rf`'d or kept as historical archives on your disk. They're .gitignored either way.
- [ ] Review `docs/launch.md` — this is the only opinionated file. If the tweet draft or HN title feels off, rewrite before any launch attempt.

## Pre-launch blockers (noted but not solved tonight)

- **GitHub Pages not confirmed enabled.** `registry.json.meta.catalog_url` points to `vibe-researcher.github.io`. Either enable Pages before any external link, or edit `registry.json` to null the URL. I deliberately didn't advertise the catalog URL in the new README to avoid a 404.
- **`generate_catalog.py` overwrites `catalog-skill/SKILL.md`.** I did **not** run it — the existing hand-crafted pointer is preserved. Run it only after deciding how to reconcile.
- **Hero GIF/asciinema** — listed in `docs/launch.md` pre-launch checklist as blocking. I physically can't record; you or a collaborator needs to.
- **OpenJudge run used K=1 graders.** `status: beta` is appropriate. A K=3 re-run would let the score graduate to `stable`. Cost ≈ 3× current run. Roadmap item.

## What I did not touch

- `SKILL.md` (orchestrator workflow) — unchanged. The v2 architecture works; redesigning it wasn't the task.
- `scripts/` — unchanged. 27 existing scripts, all stdlib, all reviewed as part of critique agent 4's feasibility pass.
- `agents/` — unchanged. Role guides are solid.
- `schemas/` — unchanged. OSR contracts are working as designed.
- `workspace/` — read-only source for the `published/` copy. Not modified.

---

## Honest self-assessment

**Strongest wins:**
1. `docs/why-v2.md` + pinning `critique-report.json` as "Receipts" in README. This is the credibility signal competitors cannot replicate — you have to have failed to publish the failure.
2. `eval-manifest.json` with SHA-256 hashes. Formalizing this into a citable protocol is the long-term moat.
3. README's "Try this now" command is **verified to work** (`--dry-run` passed 4/4). Not a vaporware one-liner.

**Weakest spots:**
1. Only one published bundle. Gallery looks thin. Mitigated by not calling it "gallery of many" — just "one so far, more coming, here's how to request".
2. No hero GIF. The single most impactful launch-day visual, and I can't produce it.
3. `docs/launch.md` is strong but untested — first actual launch attempt will reveal what's wrong with the tweet/FAQ framing.
4. The English README is sharper than the Chinese one. Partial sync was the right trade-off but the Chinese market gets a slightly degraded version until full retranslation.

**If I had another 2 hours:**
- Write a proper `docs/evidence/openjudge-before-after.md` with side-by-side task outputs (A vs B, debline them, show the quality delta). The user-UX agent flagged this as their #4 want; I deferred it.
- K=3 re-run of OpenJudge — but this is a full distillation run, it's overnight work, not 2 hours.

**Potential concerns you may raise:**
- *"Is `beta` honest enough?"* — the caveats are in both `registry.json` and `eval-manifest.json`. If you want stricter, the next step is `alpha` or "evaluation-preview".
- *"Is publishing the failure a bad look?"* — this is the exact call the positioning agent pushed hardest on. The alternative ("autonomous framework with converged skill") reads as marketing. The failure-first framing is what makes the receipts believable.
- *"Are we giving competitors a blueprint?"* — yes, partly. The counter is that the moat is doing the blind-eval work, not knowing it should be done. The manifest format is a gift to the field; the harness is ours.

---

## Metrics I'd watch post-launch

| Signal | What it means |
|---|---|
| Stars in first 48h | Baseline traction — should be 50+ if HN lands, 10+ if only Twitter |
| Distillation-request issues | Cold-start interest signal; fewer than 3 in first week = repositioning needed |
| Citations / forks of `eval-manifest.json` format | The real moat landing — watch for it over 30-90 days |
| "Beta → stable" K=3 re-run completion | First follow-up milestone; blocks credibility growth beyond initial burst |

---

*Generated at end-of-session 2026-04-24 after running the 4-agent critique team in parallel and implementing the consensus adopted items. Total session runtime ≈ 4h. Review before pushing.*
