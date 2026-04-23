#!/usr/bin/env python3
"""Validate a Skill directory against the Anthropic SKILL.md standard.

Usage:
    python scripts/validate_skill.py <skill-dir> [--changes-file <path>]
                                                  [--workspace <ws>]
                                                  [--strict]

What it checks (errors — block publish):
  * SKILL.md exists, has a valid YAML frontmatter block.
  * Required frontmatter keys: `name`, `description`.
  * name: matches ^[a-z0-9](-?[a-z0-9])*$, <=64 chars, matches directory name.
  * description: non-empty, <=1024 chars.
  * Frontmatter top-level keys are restricted to an Anthropic-recognized set
    (name, description, license, version). A legacy `metadata:` block (seen
    in older skill-anything output) is reported as an error with a migration
    suggestion — move vendor fields into a sibling PROVENANCE.yaml.
  * Markdown links of the form [text](references/... | scripts/... | assets/...
    | examples/...) resolve to existing paths inside the skill directory.
  * If PROVENANCE.yaml exists, it must be a YAML mapping (otherwise error).

What it checks (warnings — don't block):
  * Body > 500 lines (Anthropic soft limit).
  * references/*.md individually > 1000 lines.
  * description doesn't start with a third-person trigger idiom
    ("Use this skill", "This skill should be used", "Call this skill",
    "Use when", "This skill provides" ...). Anthropic's own skills use this
    pattern so Claude's triggering heuristic picks them up reliably.

With --changes-file, also invokes scripts/overfit_check.py and merges its
result. With --strict, warnings also cause a non-zero exit.

Prints JSON to stdout. Exit 0 iff no errors (warnings ignored unless
--strict). Exit 1 on validation failure. Exit 2 on invocation error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    print(json.dumps({"passed": False, "errors": [
        "PyYAML is required (pip install pyyaml)"]}))
    sys.exit(2)


NAME_PATTERN = re.compile(r"^[a-z0-9](-?[a-z0-9])*$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_LINES = 500
MAX_REFERENCE_LINES = 1000

ALLOWED_FRONTMATTER_KEYS = {"name", "description", "license", "version"}
# Look for any explicit "when to trigger" idiom anywhere in the description.
# Anthropic's own skills vary the opening (imperative verbs are fine) but
# virtually all include one of these to make triggering reliable.
DESCRIPTION_TRIGGER_PATTERNS = [
    r"\buse\s+\w+\s+when\b",         # "use this when", "use it when"
    r"\bused\s+(when|whenever|for|to)\b",
    r"\bshould\s+be\s+used\b",
    r"\bshould\s+use\s+this\s+skill\b",
    r"\btrigger\w*\b",
    r"\bwhenever\b",
    r"\bapplies\s+to\b",
    r"\bcall\s+this\s+skill\b",
]

REF_LINK_RE = re.compile(
    r"\[[^\]]*?\]\(((?:references|scripts|assets|examples)/[^)]+)\)"
)


def _split_frontmatter(content: str) -> tuple[str, str, list[str]]:
    """Return (frontmatter_text, body_text, errors).

    An empty frontmatter_text means no frontmatter was found (error is
    appended to the returned errors list).
    """
    lines = content.split("\n")
    errors: list[str] = []
    if not lines or lines[0].strip() != "---":
        errors.append("Missing YAML frontmatter (file must start with '---')")
        return "", content, errors

    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        errors.append("Malformed YAML frontmatter (no closing '---')")
        return "", content, errors

    fm = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])
    return fm, body, errors


def _check_description_style(description: str) -> list[str]:
    """Return warnings (empty list if the description looks fine)."""
    warnings: list[str] = []
    lower = description.strip().lower()
    if not any(re.search(p, lower) for p in DESCRIPTION_TRIGGER_PATTERNS):
        warnings.append(
            "description contains no explicit trigger idiom (e.g. "
            "'Use this skill when...', 'Use when...', 'Triggers include...'). "
            "Claude's triggering heuristic relies on a when/trigger phrase; "
            "consider adding one."
        )
    if len(description) < 40:
        warnings.append(
            f"description is short ({len(description)} chars); skills with "
            "richer WHAT+WHEN descriptions trigger more reliably"
        )
    return warnings


def _validate_references(skill_dir: Path) -> tuple[list[str], list[dict]]:
    """Check references/*.md line counts. Returns (warnings, summaries)."""
    warnings: list[str] = []
    summaries: list[dict] = []
    refs_dir = skill_dir / "references"
    if not refs_dir.is_dir():
        return warnings, summaries
    for md in sorted(refs_dir.rglob("*.md")):
        line_count = sum(1 for _ in md.open(encoding="utf-8", errors="replace"))
        summaries.append({
            "path": str(md.relative_to(skill_dir)),
            "lines": line_count,
        })
        if line_count > MAX_REFERENCE_LINES:
            warnings.append(
                f"{md.relative_to(skill_dir)}: {line_count} lines "
                f"(recommended <= {MAX_REFERENCE_LINES}; >300 should include a TOC)"
            )
    return warnings, summaries


def _validate_provenance(skill_dir: Path) -> tuple[list[str], dict | None]:
    """If PROVENANCE.yaml exists, validate it is a mapping. Returns
    (errors, parsed-or-None)."""
    errors: list[str] = []
    p = skill_dir / "PROVENANCE.yaml"
    if not p.exists():
        return errors, None
    try:
        parsed = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        errors.append(f"PROVENANCE.yaml: invalid YAML: {e}")
        return errors, None
    if not isinstance(parsed, dict):
        errors.append("PROVENANCE.yaml: top-level must be a mapping")
        return errors, None
    return errors, parsed


def validate(skill_dir: Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return {"passed": False, "errors": ["SKILL.md not found"], "warnings": []}

    content = skill_md.read_text(encoding="utf-8")
    fm_text, body, fm_errors = _split_frontmatter(content)
    errors.extend(fm_errors)

    # Parse frontmatter YAML
    frontmatter: dict = {}
    if fm_text:
        try:
            parsed = yaml.safe_load(fm_text) or {}
            if not isinstance(parsed, dict):
                errors.append("YAML frontmatter must be a mapping")
            else:
                frontmatter = parsed
        except yaml.YAMLError as e:
            errors.append(f"YAML frontmatter parse error: {e}")

    # Field whitelist + legacy metadata detection
    unknown_keys = sorted(set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS)
    if "metadata" in unknown_keys:
        errors.append(
            "Frontmatter contains a legacy 'metadata:' block — Anthropic "
            "Skills spec recognizes only top-level fields. Move "
            "sa-source-repo / sa-generated-at / author into a sibling "
            "PROVENANCE.yaml, and promote 'version' to a top-level key."
        )
        unknown_keys = [k for k in unknown_keys if k != "metadata"]
    if unknown_keys:
        errors.append(
            f"Unrecognized frontmatter keys: {unknown_keys}. "
            f"Allowed: {sorted(ALLOWED_FRONTMATTER_KEYS)}."
        )

    name = str(frontmatter.get("name", "")).strip()
    description = str(frontmatter.get("description", "")).strip()
    version = frontmatter.get("version")
    license_ = frontmatter.get("license")

    # name
    if not name:
        errors.append("Missing 'name' in frontmatter")
    else:
        if not NAME_PATTERN.match(name):
            errors.append(
                f"Name '{name}' does not match ^[a-z0-9](-?[a-z0-9])*$"
            )
        if len(name) > MAX_NAME_LENGTH:
            errors.append(
                f"Name too long ({len(name)} > {MAX_NAME_LENGTH})"
            )
        if skill_dir.name != name:
            errors.append(
                f"Directory name '{skill_dir.name}' does not match "
                f"skill name '{name}'"
            )

    # description
    if not description:
        errors.append("Missing 'description' in frontmatter")
    else:
        if len(description) > MAX_DESCRIPTION_LENGTH:
            errors.append(
                f"Description too long ({len(description)} > "
                f"{MAX_DESCRIPTION_LENGTH})"
            )
        warnings.extend(_check_description_style(description))

    # version / license types
    if version is not None and not isinstance(version, str):
        errors.append(
            "version must be a string (e.g. \"1.0.0\"), "
            f"got {type(version).__name__}"
        )
    if license_ is not None and not isinstance(license_, str):
        errors.append("license must be a string")

    # Body line count
    body_lines = body.split("\n")
    if len(body_lines) > MAX_BODY_LINES:
        warnings.append(
            f"Body has {len(body_lines)} lines (soft limit {MAX_BODY_LINES}; "
            "consider splitting content into references/*.md)"
        )

    # Referenced-file existence
    for link in REF_LINK_RE.findall(content):
        # Strip any '#anchor' fragment
        target = link.split("#", 1)[0]
        ref_path = skill_dir / target
        if not ref_path.exists():
            errors.append(f"Referenced file not found: {target}")

    # references/*.md line counts
    ref_warnings, ref_summary = _validate_references(skill_dir)
    warnings.extend(ref_warnings)

    # PROVENANCE.yaml
    prov_errors, prov = _validate_provenance(skill_dir)
    errors.extend(prov_errors)

    return {
        "passed": len(errors) == 0,
        "name": name,
        "description_length": len(description),
        "body_lines": len(body_lines),
        "frontmatter_keys": sorted(frontmatter.keys()),
        "version": version,
        "license": license_,
        "references": ref_summary,
        "has_provenance": prov is not None,
        "errors": errors,
        "warnings": warnings,
    }


def _run_overfit_check(changes_file: Path, workspace: Path) -> dict:
    script = Path(__file__).parent / "overfit_check.py"
    if not script.exists():
        return {"ok": False, "error": "overfit_check.py not found"}
    cmd = [
        sys.executable, str(script),
        "--workspace", str(workspace),
        "--changes-file", str(changes_file),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout) if r.stdout else {
            "ok": False, "error": r.stderr.strip() or "no output"}
    except Exception as e:
        return {"ok": False, "error": f"overfit_check invocation failed: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser(prog="validate_skill")
    ap.add_argument("skill_dir", type=Path)
    ap.add_argument("--changes-file", type=Path, default=None,
                    help="Path to a Skill Writer OSR (or raw changes JSON) — "
                         "also runs overfit_check.py")
    ap.add_argument("--workspace", type=Path, default=None,
                    help="Workspace root (defaults to 2 parents up from skill-dir)")
    ap.add_argument("--strict", action="store_true",
                    help="Treat warnings as errors for exit code purposes")
    args = ap.parse_args()

    skill_dir = args.skill_dir.resolve()
    if not skill_dir.is_dir():
        print(json.dumps({"passed": False,
                          "errors": [f"Not a directory: {args.skill_dir}"]}))
        return 2

    result = validate(skill_dir)

    if args.changes_file:
        workspace = args.workspace or args.skill_dir.parent.parent
        of = _run_overfit_check(args.changes_file, workspace)
        result["overfit_check"] = of
        if not of.get("ok", False):
            result["passed"] = False
            result.setdefault("errors", []).append(
                f"overfit_check failed: {of.get('failed', 0)} of "
                f"{of.get('total_changes', 0)} changes rejected"
            )

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["passed"]:
        return 1
    if args.strict and result.get("warnings"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
