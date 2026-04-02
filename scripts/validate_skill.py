#!/usr/bin/env python3
"""Validate a Skill directory against the Anthropic SKILL.md standard.

Usage:
    python scripts/validate_skill.py <skill-dir>

Checks name format, description, line count, and file references.
Prints JSON with pass/fail results.
"""

import json
import re
import sys
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9](-?[a-z0-9])*$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_LINES = 500


def validate(skill_dir: Path) -> dict:
    errors = []
    warnings = []

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return {"passed": False, "errors": ["SKILL.md not found"], "warnings": []}

    content = skill_md.read_text()
    lines = content.split("\n")

    # Parse YAML frontmatter
    name = ""
    description = ""
    if lines[0].strip() == "---":
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx:
            frontmatter = "\n".join(lines[1:end_idx])
            for line in frontmatter.split("\n"):
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    desc_line = line.split(":", 1)[1].strip()
                    if desc_line.startswith(">-") or desc_line.startswith("|"):
                        desc_lines = []
                        for j in range(frontmatter.split("\n").index(line) + 1, len(frontmatter.split("\n"))):
                            fl = frontmatter.split("\n")[j]
                            if fl and fl[0] == " ":
                                desc_lines.append(fl.strip())
                            else:
                                break
                        description = " ".join(desc_lines)
                    else:
                        description = desc_line.strip('"').strip("'")
            body_lines = lines[end_idx + 1:]
        else:
            errors.append("Malformed YAML frontmatter (no closing ---)")
            body_lines = lines
    else:
        errors.append("Missing YAML frontmatter")
        body_lines = lines

    # Check name
    if not name:
        errors.append("Missing 'name' in frontmatter")
    elif not NAME_PATTERN.match(name):
        errors.append(f"Name '{name}' does not match [a-z0-9](-?[a-z0-9])* pattern")
    elif len(name) > MAX_NAME_LENGTH:
        errors.append(f"Name too long ({len(name)} > {MAX_NAME_LENGTH})")

    if name and skill_dir.name != name:
        errors.append(f"Directory name '{skill_dir.name}' does not match skill name '{name}'")

    # Check description
    if not description:
        errors.append("Missing 'description' in frontmatter")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"Description too long ({len(description)} > {MAX_DESCRIPTION_LENGTH})")

    # Check body line count
    non_empty_body = [l for l in body_lines if l.strip()]
    if len(body_lines) > MAX_BODY_LINES:
        warnings.append(f"Body has {len(body_lines)} lines (recommended < {MAX_BODY_LINES})")

    # Check referenced files exist
    for line in lines:
        # Match markdown links like [text](references/xxx.md) or [text](scripts/xxx.py)
        for match in re.finditer(r"\[.*?\]\(((?:references|scripts|assets|examples)/[^)]+)\)", line):
            ref_path = skill_dir / match.group(1)
            if not ref_path.exists():
                errors.append(f"Referenced file not found: {match.group(1)}")

    return {
        "passed": len(errors) == 0,
        "name": name,
        "description_length": len(description),
        "body_lines": len(body_lines),
        "errors": errors,
        "warnings": warnings,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_skill.py <skill-dir>", file=sys.stderr)
        sys.exit(1)

    skill_dir = Path(sys.argv[1])
    if not skill_dir.is_dir():
        print(json.dumps({"passed": False, "errors": [f"Not a directory: {skill_dir}"]}))
        sys.exit(1)

    result = validate(skill_dir)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
