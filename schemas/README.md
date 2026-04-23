# Schemas

JSON Schema (Draft 2020-12) contracts for skill-anything v2. Every cross-boundary JSON artifact produced during a distillation run is validated against one of these schemas by `scripts/osr_validate.py` (agent returns), `scripts/state_manager.py` (state files), or `scripts/deblind_and_score.py` (grader output).

## Why schemas exist

The v1 loop collapsed because sub-agent returns were natural language; the Orchestrator could neither mechanically trust them nor recover from session compaction. These schemas give us three things:

1. **Mechanical trust**: a hook can validate a return the moment it arrives and reject garbage before it contaminates state.
2. **Markov recovery**: state files have a fixed, validated shape. On restart the Orchestrator loads `state.json`, reads one event, and acts — no reliance on context memory.
3. **Controlled extensibility**: every OSR schema allows `additionalProperties: true` at the root. Agents can invent new fields when the schema under-specifies their findings; those fields are preserved in `extras` and feed schema evolution.

Read that last point carefully: **schemas are the information *lower bound*, not upper bound.** An agent that discovers something the schema doesn't have a field for should still report it — via `surprises`, `meta_observations`, `notes_path`, `requested_schema_extension`, or `extras`.

## File map

| File | Purpose | Validated by |
|------|---------|--------------|
| `state.schema.json` | `workspace/state.json` (L1 Orchestrator state) | `state_manager.py` |
| `iteration.schema.json` | `workspace/state/iterations/iter-N.json` (L2 per-iteration detail) | `state_manager.py` |
| `event.schema.json` | One line of `workspace/state/events.jsonl` | `state_manager.py append-event` |
| `eval-task.schema.json` | One entry in `workspace/evals/eval-tasks.json` | Orchestrator before Run phase |
| `osr-common.schema.json` | **Reference only** — describes the shared OSR contract | Humans, not code |
| `osr-researcher.schema.json` | Researcher sub-agent return | `osr_validate.py --agent researcher` |
| `osr-skill-writer.schema.json` | Skill Writer sub-agent return | `osr_validate.py --agent skill_writer` |
| `osr-eval-designer.schema.json` | Eval Designer sub-agent return | `osr_validate.py --agent eval_designer` |
| `osr-runner.schema.json` | Runner sub-agent return (one per variant) | `osr_validate.py --agent runner` |
| `osr-grader.schema.json` | Grader sub-agent return | `osr_validate.py --agent grader` |

`osr-common.schema.json` is documentation. Each agent schema inlines the common fields so the validator needs no `$ref` resolution across files. When you change common-field definitions, propagate by hand to all `osr-*` schemas and bump versions.

## OSR contract (all agents)

Every OSR includes:

- **Required control fields** (agent-specific business output — the things the Orchestrator mechanically consumes to decide next_action)
- **Open channels** (`surprises`, `anomalies`, `meta_observations`) — short signals about things the agent found worth flagging that aren't in the required fields
- **Overflow reference** (`notes_path`, `notes_topic_tags`) — long observations go to disk; the OSR only names them
- **Schema escape hatch** (`status: "schema_insufficient"` + `requested_schema_extension`) — when the schema cannot represent the finding
- **Extras** (untyped pocket) — any additional fields are preserved, not stripped

See `osr-common.schema.json` for the canonical shape.

## Versioning policy

- Version field: `schema_version` in `state.schema.json` (tracks the whole schema set). Current: `2.0.0`.
- **Major** bump: remove a required field, change a field type, tighten an enum. Breaks prior state files.
- **Minor** bump: add a new required field with a default, add a new optional field, widen an enum. Old state files need a migration (`scripts/state_migrate.py`).
- **Patch** bump: update descriptions, tighten validation without rejecting any previously-valid payloads.

A schema change should be accompanied by:

1. A version bump in all affected schemas.
2. An update to the corresponding agent markdown file in `agents/`.
3. If the change affects on-disk files (state, iteration, events), a migration path in `state_migrate.py`.

## Evolution via extras

When an agent frequently populates a specific `extras` key across runs, that's evidence the schema should formalize the field. `scripts/invariant_check.py` periodically scans `events.jsonl` for recurring `extras` keys and surfaces them to the Orchestrator as meta-observations. A human maintainer decides whether to promote the field into the next schema minor version.

This keeps schemas as crystallized consensus of what's *currently known to matter*, without freezing what agents are allowed to say.

## Anti-flat-pipeline guards

Three schema-level features prevent the system from degenerating into a brittle pipeline that can only handle pre-conceived cases:

1. **Open channels are required** (not optional). An empty `surprises` array is valid JSON, but `scripts/invariant_check.py` flags a Grader or Skill Writer who never files a surprise across N consecutive iterations — silence is treated as a health signal.
2. **`requested_schema_extension` is a first-class response.** When an agent returns `status: "schema_insufficient"`, the Orchestrator is required to either (a) rerun with a widened prompt, or (b) accept the extension and update the schema. It cannot silently ignore.
3. **`additionalProperties: true`** on every OSR root. Unknown fields are preserved in state; they do not bounce the return. This is the mechanical guarantee that schemas are an information lower bound.
