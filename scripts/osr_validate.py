#!/usr/bin/env python3
"""Validate an OSR (Open-Structured Return) JSON against its agent schema.

This is a *minimal* JSON Schema (Draft 2020-12) validator implementing exactly
the subset of keywords used by skill-anything's OSR schemas:

    type, required, properties, additionalProperties (boolean),
    items, enum, const, pattern, minLength, maxLength,
    minimum, maximum, minItems, maxItems,
    oneOf, anyOf, allOf, $ref (local '#/$defs/...'), $defs

No external dependencies. Catches malformed OSRs early so bad sub-agent
returns never contaminate state.

Usage:
    python scripts/osr_validate.py --agent <role> --input <path>
    python scripts/osr_validate.py --schema <path> --input <path>
    python scripts/osr_validate.py --agent runner --input -         # stdin

Exit codes:
    0 — validation passed
    1 — validation failed (details printed as JSON on stdout + human text on stderr)
    2 — invocation / IO error

Also enforces cross-field invariants:
    * status=schema_insufficient REQUIRES requested_schema_extension to be present
    * Skill Writer: every add/modify change MUST have non-empty knowledge_source_refs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


# ---------------------------------------------------------------------------
# Minimal JSON Schema validator
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    def __init__(self, path: str, msg: str, suggestion: str = ""):
        super().__init__(msg)
        self.path = path
        self.msg = msg
        self.suggestion = suggestion

    def as_dict(self) -> dict:
        d = {"path": self.path or "/", "error": self.msg}
        if self.suggestion:
            d["suggestion"] = self.suggestion
        return d


def _type_of(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def _resolve_ref(schema: dict, ref: str, root: dict) -> dict:
    """Resolve a $ref of the form '#/$defs/Name' within root schema."""
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported non-local $ref: {ref}")
    parts = ref[2:].split("/")
    cur: Any = root
    for p in parts:
        if p not in cur:
            raise ValueError(f"cannot resolve $ref {ref} (missing '{p}')")
        cur = cur[p]
    return cur


def _validate(value: Any, schema: dict, root: dict, path: str, errors: list) -> None:
    """Validate `value` against `schema`. Append any ValidationError to errors."""
    if not isinstance(schema, dict):
        return  # permissive: non-schema means "any"

    if "$ref" in schema:
        try:
            resolved = _resolve_ref(schema, schema["$ref"], root)
        except ValueError as e:
            errors.append(ValidationError(path, str(e)))
            return
        _validate(value, resolved, root, path, errors)
        return

    # Type check
    if "type" in schema:
        expected = schema["type"]
        expected_list = [expected] if isinstance(expected, str) else list(expected)
        actual = _type_of(value)
        # "integer" is a subset of "number" in Draft 2020-12
        if actual == "integer" and "number" in expected_list:
            pass
        elif actual not in expected_list:
            errors.append(ValidationError(
                path,
                f"type mismatch: expected {expected_list}, got {actual}",
                f"change value at {path or '/'} to a {expected_list[0]}",
            ))
            return

    # const / enum
    if "const" in schema and value != schema["const"]:
        errors.append(ValidationError(
            path,
            f"const mismatch: expected {schema['const']!r}, got {value!r}",
            f"set {path} to exactly {schema['const']!r}",
        ))
    if "enum" in schema and value not in schema["enum"]:
        errors.append(ValidationError(
            path,
            f"enum violation: got {value!r}, allowed={schema['enum']}",
            f"pick one of {schema['enum']}",
        ))

    # String
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(ValidationError(
                path, f"minLength {schema['minLength']}, got {len(value)}"
            ))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(ValidationError(
                path,
                f"maxLength {schema['maxLength']}, got {len(value)}",
                f"truncate to {schema['maxLength']} chars",
            ))
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    errors.append(ValidationError(
                        path, f"pattern mismatch: {schema['pattern']!r}"
                    ))
            except re.error as e:
                errors.append(ValidationError(path, f"bad regex in schema: {e}"))

    # Numeric
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(ValidationError(path, f"minimum {schema['minimum']}, got {value}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(ValidationError(path, f"maximum {schema['maximum']}, got {value}"))

    # Array
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(ValidationError(path, f"minItems {schema['minItems']}, got {len(value)}"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(ValidationError(path, f"maxItems {schema['maxItems']}, got {len(value)}"))
        if "items" in schema:
            for i, elem in enumerate(value):
                _validate(elem, schema["items"], root, f"{path}[{i}]", errors)

    # Object
    if isinstance(value, dict):
        required = schema.get("required", [])
        for k in required:
            if k not in value:
                errors.append(ValidationError(
                    path, f"missing required field '{k}'",
                    f"add '{k}' to object at {path or '/'}",
                ))
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        # Per-property validation
        for k, v in value.items():
            if k in props:
                _validate(v, props[k], root, f"{path}.{k}", errors)
            elif addl is False:
                errors.append(ValidationError(
                    f"{path}.{k}",
                    f"unexpected property '{k}' (additionalProperties=false)",
                    f"remove '{k}' or move it into extras",
                ))
            # addl is True -> pass (open-world); addl could also be a schema (not used here)

    # Logical combinators
    if "oneOf" in schema:
        matched = 0
        for sub in schema["oneOf"]:
            sub_errors: list = []
            _validate(value, sub, root, path, sub_errors)
            if not sub_errors:
                matched += 1
        if matched != 1:
            errors.append(ValidationError(
                path, f"oneOf: matched {matched} schemas (expected 1)",
            ))
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            sub_errors: list = []
            _validate(value, sub, root, path, sub_errors)
            if not sub_errors:
                break
        else:
            errors.append(ValidationError(path, "anyOf: no sub-schema matched"))
    if "allOf" in schema:
        for i, sub in enumerate(schema["allOf"]):
            _validate(value, sub, root, path, errors)


# ---------------------------------------------------------------------------
# Cross-field invariants (semantic checks beyond raw schema)
# ---------------------------------------------------------------------------


def _invariant_schema_insufficient(payload: dict, errors: list) -> None:
    if payload.get("status") == "schema_insufficient":
        if "requested_schema_extension" not in payload:
            errors.append(ValidationError(
                "/",
                "status=schema_insufficient requires requested_schema_extension",
                "add requested_schema_extension object with field_name/field_type/reason",
            ))


def _invariant_skill_writer_refs(payload: dict, errors: list) -> None:
    if payload.get("agent_type") != "skill_writer":
        return
    for i, ch in enumerate(payload.get("changes_applied") or []):
        ctype = ch.get("type")
        refs = ch.get("knowledge_source_refs") or []
        if ctype in ("add", "modify") and not refs:
            errors.append(ValidationError(
                f"changes_applied[{i}].knowledge_source_refs",
                f"change type={ctype} MUST have non-empty knowledge_source_refs",
                "cite at least one (path, line_from, line_to) from knowledge/*.md — "
                "this is the anti-overfitting contract",
            ))


def _invariant_runner_files_read(payload: dict, errors: list) -> None:
    if payload.get("agent_type") != "runner":
        return
    variant = payload.get("variant")
    files_read = payload.get("files_read") or []
    if variant == "without_skill":
        leaks = [f for f in files_read if "skills/" in f or f.startswith("skills")]
        if leaks:
            errors.append(ValidationError(
                "files_read",
                f"without_skill Runner read skill files: {leaks}",
                "physical isolation broken — Runner should not see skills/",
            ))


def _invariant_eval_designer_isolation(payload: dict, errors: list) -> None:
    if payload.get("agent_type") != "eval_designer":
        return
    iso = payload.get("isolation_env_path", "")
    if "skills" in Path(iso).parts if iso else False:
        errors.append(ValidationError(
            "isolation_env_path",
            f"isolation_env_path {iso!r} contains 'skills' — isolation breach",
        ))


INVARIANTS = [
    _invariant_schema_insufficient,
    _invariant_skill_writer_refs,
    _invariant_runner_files_read,
    _invariant_eval_designer_isolation,
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


AGENT_SCHEMA_MAP = {
    "researcher": "osr-researcher.schema.json",
    "skill_writer": "osr-skill-writer.schema.json",
    "eval_designer": "osr-eval-designer.schema.json",
    "runner": "osr-runner.schema.json",
    "grader": "osr-grader.schema.json",
    # Also validate non-OSR artifacts for convenience
    "state": "state.schema.json",
    "iteration": "iteration.schema.json",
    "event": "event.schema.json",
    "eval_task": "eval-task.schema.json",
}


def validate_against_schema(payload: Any, schema_path: Path) -> list:
    """Return list[ValidationError]. Empty list means valid."""
    schema = json.loads(schema_path.read_text())
    errors: list[ValidationError] = []
    _validate(payload, schema, schema, "", errors)
    # Semantic invariants (only for dicts)
    if isinstance(payload, dict):
        for inv in INVARIANTS:
            inv(payload, errors)
    return errors


def load_input(path: str) -> Any:
    if path == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text())


def main() -> int:
    ap = argparse.ArgumentParser(prog="osr_validate")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", choices=list(AGENT_SCHEMA_MAP.keys()))
    group.add_argument("--schema", type=Path,
                       help="Path to a schema file (for ad-hoc validation)")
    ap.add_argument("--input", required=True,
                    help="Path to JSON file to validate, or '-' for stdin")
    ap.add_argument("--quiet", action="store_true",
                    help="Exit code only; no output on success")
    args = ap.parse_args()

    try:
        payload = load_input(args.input)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": f"could not load input: {e}"}))
        print(f"ERROR: could not load input: {e}", file=sys.stderr)
        return 2

    if args.agent:
        schema_path = SCHEMAS_DIR / AGENT_SCHEMA_MAP[args.agent]
    else:
        schema_path = args.schema
    if not schema_path.exists():
        print(json.dumps({"ok": False, "error": f"schema not found: {schema_path}"}))
        return 2

    errors = validate_against_schema(payload, schema_path)

    if errors:
        result = {
            "ok": False,
            "schema": str(schema_path.name),
            "error_count": len(errors),
            "errors": [e.as_dict() for e in errors[:20]],  # cap for readability
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        for e in errors[:20]:
            hint = f" [{e.suggestion}]" if e.suggestion else ""
            print(f"  {e.path or '/'}: {e.msg}{hint}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more", file=sys.stderr)
        return 1

    if not args.quiet:
        print(json.dumps({"ok": True, "schema": schema_path.name}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
