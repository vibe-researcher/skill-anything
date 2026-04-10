#!/usr/bin/env python3
"""Generate catalog SKILL.md from registry.json.

Produces a compact, browsable catalog of all published Skills.
Deployed to GitHub Pages for agent discovery via URL fetch.
"""
import json
import sys
from pathlib import Path


def main():
    repo_root = Path(__file__).parent.parent
    registry_path = repo_root / "registry.json"
    output_path = repo_root / "catalog-skill" / "SKILL.md"
    docs_output = repo_root / "docs" / "catalog" / "SKILL.txt"

    with open(registry_path) as f:
        data = json.load(f)

    skills = data["skills"]
    repo_url = data["meta"]["repo"]

    lines = [
        "---",
        "name: skill-anything-catalog",
        "description: >-",
        f"  Browse {len(skills)} distilled Agent Skills. Each Skill was produced by",
        "  skill-anything's research → generate → evaluate → iterate pipeline.",
        "---",
        "",
        "# skill-anything Catalog",
        "",
        f"Distilled Agent Skills for {len(skills)} repositories.",
        "Each Skill was validated through blind A/B evaluation against a no-Skill baseline.",
        "",
        "## Available Skills",
        "",
        "| Repo | Description | Score | Skills | Install |",
        "|------|-------------|-------|--------|---------|",
    ]

    for s in sorted(skills, key=lambda x: x["name"]):
        name = s["name"]
        desc = s.get("description", "")[:80]
        score = f"{s['score']:.2f}" if s.get("score") is not None else "—"
        count = s.get("skill_count", 1)
        install = f"`git clone {repo_url} && cd published/{name}/`"
        lines.append(f"| **{name}** | {desc} | {score} | {count} | {install} |")

    lines.extend([
        "",
        "## How to Use",
        "",
        "```bash",
        "# Clone the repo and copy the Skill you need",
        f"git clone {repo_url}",
        "cp -r published/<name>/ ~/.cursor/skills/",
        "",
        "# Or fetch this catalog to discover available Skills",
        f"# URL: {data['meta'].get('catalog_url', 'N/A')}",
        "```",
        "",
        "## Skill Details",
        "",
    ])

    for s in sorted(skills, key=lambda x: x["name"]):
        lines.append(f"### {s['name']}")
        lines.append("")
        lines.append(f"- **Source**: {s['source_repo']}")
        lines.append(f"- **Score**: {s.get('score', 'N/A')}")
        lines.append(f"- **Iterations**: {s.get('iterations', 'N/A')}")
        lines.append(f"- **Published**: {s.get('published_at', 'N/A')}")

        for sub in s.get("skills", []):
            lines.append(f"- `{sub['name']}`: {sub.get('description', '')[:60]}")

        lines.append("")

    lines.extend([
        "## More Info",
        "",
        f"- Repository: {repo_url}",
        f"- Last Updated: {data['meta'].get('updated', 'N/A')}",
    ])

    content = "\n".join(lines) + "\n"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)

    docs_output.parent.mkdir(parents=True, exist_ok=True)
    docs_output.write_text(content)

    print(f"Generated catalog with {len(skills)} skills at {output_path}")
    print(f"Copied to {docs_output} for GitHub Pages deployment")


if __name__ == "__main__":
    main()
