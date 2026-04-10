#!/usr/bin/env python3
"""Register completed Skills from a workspace into registry.json.

Reads workspace/skills/ and workspace/results.tsv, copies Skill files
to published/<name>/, and updates registry.json with metadata.
"""
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def read_results_tsv(workspace: Path) -> dict:
    """Extract final score and iteration count from results.tsv."""
    tsv = workspace / "results.tsv"
    if not tsv.exists():
        return {"score": None, "iterations": 0, "cost": 0.0}

    lines = tsv.read_text().strip().split("\n")
    kept = [l for l in lines[1:] if l.split("\t")[3] == "keep"] if len(lines) > 1 else []
    if not kept:
        return {"score": None, "iterations": len(lines) - 1, "cost": 0.0}

    last = kept[-1].split("\t")
    return {
        "score": float(last[1]) if last[1] else None,
        "iterations": len(lines) - 1,
        "cost": sum(float(l.split("\t")[2]) for l in lines[1:] if l.split("\t")[2]),
    }


def find_skill_dirs(skills_root: Path) -> list[Path]:
    """Find all Skill directories (those containing SKILL.md)."""
    dirs = []
    for p in skills_root.iterdir():
        if p.is_dir() and (p / "SKILL.md").exists():
            dirs.append(p)
    return sorted(dirs)


def extract_frontmatter(skill_md: Path) -> dict:
    """Extract YAML frontmatter fields from a SKILL.md."""
    text = skill_md.read_text()
    if not text.startswith("---"):
        return {}

    end = text.index("---", 3)
    fm = {}
    for line in text[3:end].strip().split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in ("name", "description"):
                fm[key] = val
    return fm


def main():
    if len(sys.argv) < 3:
        print("Usage: python register_skill.py <workspace> <source-repo-url> [--name override-name]", file=sys.stderr)
        sys.exit(1)

    workspace = Path(sys.argv[1])
    source_repo = sys.argv[2]
    override_name = None

    if "--name" in sys.argv:
        idx = sys.argv.index("--name")
        if idx + 1 < len(sys.argv):
            override_name = sys.argv[idx + 1]

    skills_root = workspace / "skills"
    if not skills_root.exists():
        print(f"Error: {skills_root} not found", file=sys.stderr)
        sys.exit(1)

    repo_root = Path(__file__).parent.parent
    registry_path = repo_root / "registry.json"
    published_root = repo_root / "published"

    with open(registry_path) as f:
        registry = json.load(f)

    skill_dirs = find_skill_dirs(skills_root)
    if not skill_dirs:
        print("Error: no Skill directories found in workspace/skills/", file=sys.stderr)
        sys.exit(1)

    repo_name = override_name or source_repo.rstrip("/").split("/")[-1].lower()
    results = read_results_tsv(workspace)

    dest = published_root / repo_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(skills_root, dest)

    entry = {
        "name": repo_name,
        "source_repo": source_repo,
        "description": "",
        "version": "1.0",
        "score": results["score"],
        "iterations": results["iterations"],
        "cost_usd": round(results["cost"], 2),
        "skill_count": len(skill_dirs),
        "skills": [],
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "path": f"published/{repo_name}/",
    }

    for sd in skill_dirs:
        fm = extract_frontmatter(sd / "SKILL.md")
        entry["skills"].append({
            "name": fm.get("name", sd.name),
            "description": fm.get("description", ""),
            "path": f"published/{repo_name}/{sd.name}/SKILL.md",
        })

    if not entry["description"] and entry["skills"]:
        entry["description"] = entry["skills"][0]["description"]

    registry["skills"] = [s for s in registry["skills"] if s["name"] != repo_name]
    registry["skills"].append(entry)
    registry["meta"]["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Registered {repo_name}: {len(skill_dirs)} skill(s), score={results['score']}, path={dest}")


if __name__ == "__main__":
    main()
