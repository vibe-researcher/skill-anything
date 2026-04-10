---
name: skill-anything-catalog
description: >-
  Discover distilled Agent Skills for popular frameworks and tools.
  Access the live catalog to find Skills produced by skill-anything.
---

# skill-anything Catalog

Browse distilled Agent Skills that were produced by skill-anything's research → evaluate → iterate pipeline.

## Live Catalog

**URL**: [`https://vibe-researcher.github.io/skill-anything/SKILL.txt`](https://vibe-researcher.github.io/skill-anything/SKILL.txt)

The catalog is auto-updated on each publish and provides:
- Full list of available Skills with evaluation scores
- Source repo links and iteration counts
- Install instructions

**Note**: The file is served as `.txt` but contains markdown formatting.

## How to Use

1. **Fetch the catalog**: read the URL above (markdown format)
2. **Find your Skill**: browse by repo name or description
3. **Install**: clone the repo and copy `published/<name>/` to your skills directory

## Example

```bash
# Fetch catalog to see available Skills
curl -s https://vibe-researcher.github.io/skill-anything/SKILL.txt

# Install a specific Skill
git clone https://github.com/vibe-researcher/skill-anything
cp -r published/mcp-sdk/ ~/.cursor/skills/
```

## More Info

- Repository: https://github.com/vibe-researcher/skill-anything
- Methodology: See SKILL.md in the repo root
