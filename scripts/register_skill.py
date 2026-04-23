#!/usr/bin/env python3
"""Publish a validated skill bundle to a Claude Code skill location.

Targets:
  --to user         → ~/.claude/skills/<name>/
  --to project      → <project-root>/.claude/skills/<name>/
                      (default project root is the current working directory)
  --to plugin-repo  → <plugin-repo>/skills/<name>/
                      and update <plugin-repo>/.claude-plugin/marketplace.json

Typical use:
    python3 scripts/register_skill.py workspace/skills/openjudge-grader-selection --to user
    python3 scripts/register_skill.py workspace/skills/* --to user --force
    python3 scripts/register_skill.py workspace/skills/x --to plugin-repo \\
            --plugin-repo . --plugin-name auto-distilled

Behavior:
  * Runs validate_skill.py first; any errors abort (warnings are printed but
    don't block). Pass --skip-validate to bypass.
  * Default is a recursive copy; --link creates a symlink to the source.
  * PROVENANCE.yaml is NOT copied by default — it's generator metadata,
    not something the consumer needs. Pass --include-provenance to keep it.
  * Refuses to overwrite an existing destination unless --force.
  * --dry-run prints planned actions; no filesystem writes.

Exits:
  0 — all skills published (or dry-run completed) successfully
  1 — one or more skills failed
  2 — invocation error
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print(json.dumps({"ok": False, "error": "PyYAML required"}))
    sys.exit(2)


SCRIPT_DIR = Path(__file__).resolve().parent

# Anthropic skill bundle whitelist. Anything outside this list is ignored
# when publishing — this matters for skills whose "directory" is really a
# larger repo (e.g. skill-anything itself ships SKILL.md at repo root
# alongside workspace/, scripts/, agents/, etc. that are NOT part of the
# skill bundle and must not leak into ~/.claude/skills/).
BUNDLE_ROOT_FILES = {
    "SKILL.md",
    "PROVENANCE.yaml",
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
}
BUNDLE_SUBDIRS = {"references", "scripts", "assets", "examples"}
EXCLUDED_FILENAMES = {"PROVENANCE.yaml"}


def _parse_frontmatter(skill_md: Path) -> dict:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    try:
        return yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return {}


def _run_validate(skill_dir: Path) -> dict:
    script = SCRIPT_DIR / "validate_skill.py"
    r = subprocess.run(
        [sys.executable, str(script), str(skill_dir)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"passed": False,
                "errors": [r.stderr.strip() or "validate_skill.py produced no JSON"]}


def _should_include(rel: Path) -> bool:
    """Decide whether a path inside the skill directory belongs to the
    Anthropic skill bundle. Entries that are neither a whitelisted root
    file nor inside a whitelisted subdir are skipped. This keeps ambient
    repo files (git metadata, workspace/, etc.) out of the published
    bundle."""
    parts = rel.parts
    if not parts:
        return False
    top = parts[0]
    if len(parts) == 1:
        return top in BUNDLE_ROOT_FILES or top in BUNDLE_SUBDIRS
    # Nested file: must be under a whitelisted subdir.
    return top in BUNDLE_SUBDIRS


def _publish_copy(src: Path, dst: Path, include_provenance: bool,
                  use_symlink: bool) -> tuple[int, list[str]]:
    """Perform the copy/symlink. Returns (file_count, skipped_top_level_names)."""
    if use_symlink:
        if dst.is_symlink() or dst.exists():
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src.resolve(), dst)
        # With --link we can't enforce the whitelist; caller is warned
        # separately.
        return 1, []

    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    skipped_top: set[str] = set()
    count = 0
    for entry in sorted(src.rglob("*")):
        rel = entry.relative_to(src)
        if not _should_include(rel):
            if rel.parts:
                skipped_top.add(rel.parts[0])
            continue
        if not include_provenance and rel.name in EXCLUDED_FILENAMES:
            continue
        target = dst / rel
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
            count += 1
    return count, sorted(skipped_top)


def _resolve_target(to: str, name: str, project_root: Path,
                    plugin_repo: Path | None) -> Path:
    if to == "user":
        return Path.home() / ".claude" / "skills" / name
    if to == "project":
        return project_root / ".claude" / "skills" / name
    if to == "plugin-repo":
        if plugin_repo is None:
            raise ValueError("--plugin-repo PATH required for --to plugin-repo")
        return plugin_repo / "skills" / name
    raise ValueError(f"unknown --to kind: {to}")


def _update_marketplace(plugin_repo: Path, plugin_name: str,
                        skill_names: list[str], dry_run: bool) -> dict:
    """Idempotently register skills under a named plugin entry in
    <plugin_repo>/.claude-plugin/marketplace.json. Scaffolds the file if
    absent."""
    mp_dir = plugin_repo / ".claude-plugin"
    mp_path = mp_dir / "marketplace.json"
    if mp_path.exists():
        mp = json.loads(mp_path.read_text(encoding="utf-8"))
    else:
        mp = {
            "name": plugin_repo.resolve().name,
            "owner": {"name": "", "email": ""},
            "metadata": {
                "description": "skills distilled by skill-anything",
                "version": "0.1.0",
            },
            "plugins": [],
        }
    mp.setdefault("plugins", [])
    entry = next((p for p in mp["plugins"] if p.get("name") == plugin_name), None)
    if entry is None:
        entry = {
            "name": plugin_name,
            "description": "Auto-registered by skill-anything",
            "source": "./",
            "strict": False,
            "skills": [],
        }
        mp["plugins"].append(entry)
    entry.setdefault("skills", [])
    added: list[str] = []
    for n in skill_names:
        rel = f"./skills/{n}"
        if rel not in entry["skills"]:
            entry["skills"].append(rel)
            added.append(rel)
    if not dry_run:
        mp_dir.mkdir(parents=True, exist_ok=True)
        mp_path.write_text(
            json.dumps(mp, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return {
        "marketplace_path": str(mp_path),
        "plugin_name": plugin_name,
        "added": added,
        "already_present": [
            f"./skills/{n}" for n in skill_names
            if f"./skills/{n}" not in added
        ],
    }


def publish_one(skill_dir: Path, args: argparse.Namespace) -> dict:
    original = skill_dir
    skill_dir = skill_dir.resolve()
    result: dict = {"source": str(original)}
    if not skill_dir.is_dir():
        result["ok"] = False
        result["error"] = "not a directory"
        return result

    fm = _parse_frontmatter(skill_dir / "SKILL.md")
    name = str(fm.get("name") or "").strip()
    if not name:
        result["ok"] = False
        result["error"] = "could not read 'name' from SKILL.md frontmatter"
        return result
    if skill_dir.name != name:
        result["ok"] = False
        result["error"] = (
            f"directory name '{skill_dir.name}' does not match "
            f"SKILL.md name '{name}'"
        )
        return result
    result["name"] = name

    if not args.skip_validate:
        vr = _run_validate(skill_dir)
        result["validate"] = {
            "passed": vr.get("passed", False),
            "errors": vr.get("errors", []),
            "warnings": vr.get("warnings", []),
        }
        if not vr.get("passed", False):
            result["ok"] = False
            result["error"] = "validate_skill.py reported errors"
            return result

    try:
        dst = _resolve_target(args.to, name, args.project_root, args.plugin_repo)
    except ValueError as e:
        result["ok"] = False
        result["error"] = str(e)
        return result
    result["target"] = str(dst)

    if (dst.exists() or dst.is_symlink()) and not args.force:
        result["ok"] = False
        result["error"] = f"destination exists (pass --force to overwrite): {dst}"
        return result

    if args.dry_run:
        result["ok"] = True
        result["dry_run"] = True
        mode = "symlink" if args.link else "copy"
        excl = "" if (args.include_provenance or args.link) \
            else " (excluding PROVENANCE.yaml)"
        result["would"] = f"{mode} {skill_dir} → {dst}{excl}"
        return result

    try:
        n, skipped = _publish_copy(
            skill_dir, dst,
            include_provenance=args.include_provenance,
            use_symlink=args.link,
        )
        result["ok"] = True
        result["files"] = n
        if skipped:
            result["skipped_outside_bundle"] = skipped
    except OSError as e:
        result["ok"] = False
        result["error"] = f"filesystem error: {e}"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(prog="register_skill")
    ap.add_argument("skill_dirs", type=Path, nargs="+",
                    help="One or more skill directories to publish")
    ap.add_argument("--to", required=True,
                    choices=["user", "project", "plugin-repo"],
                    help="Publish destination")
    ap.add_argument("--project-root", type=Path, default=Path.cwd(),
                    help="Project root for --to project (default: cwd)")
    ap.add_argument("--plugin-repo", type=Path, default=None,
                    help="Plugin-repo path for --to plugin-repo")
    ap.add_argument("--plugin-name", default="auto-distilled",
                    help="Plugin entry name inside marketplace.json "
                         "(default: auto-distilled)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print planned actions; no filesystem writes")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing destination")
    ap.add_argument("--link", action="store_true",
                    help="Symlink source directory instead of copying")
    ap.add_argument("--include-provenance", action="store_true",
                    help="Copy PROVENANCE.yaml too (default: excluded)")
    ap.add_argument("--skip-validate", action="store_true",
                    help="Skip validate_skill.py pre-flight")
    args = ap.parse_args()

    if args.to == "plugin-repo" and args.plugin_repo is None:
        print(json.dumps({"ok": False,
                          "error": "--plugin-repo PATH required for --to plugin-repo"}))
        return 2

    results = [publish_one(d, args) for d in args.skill_dirs]
    overall_ok = all(r.get("ok", False) for r in results)

    mp_update = None
    if args.to == "plugin-repo" and overall_ok:
        names = [r["name"] for r in results if r.get("name")]
        if names:
            mp_update = _update_marketplace(
                args.plugin_repo, args.plugin_name, names, args.dry_run,
            )

    output = {
        "ok": overall_ok,
        "dry_run": args.dry_run,
        "to": args.to,
        "results": results,
    }
    if mp_update is not None:
        output["marketplace"] = mp_update
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
