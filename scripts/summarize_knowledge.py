#!/usr/bin/env python3
"""Generate domain-summary.yaml from knowledge-map.yaml.

Produces a compact overview (domain IDs, concepts, self-sufficiency,
relationships) for downstream consumers that need structural context
without full knowledge details — e.g. Eval Designer, Skill index.
"""
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: python summarize_knowledge.py <workspace>", file=sys.stderr)
        sys.exit(1)

    workspace = Path(sys.argv[1])
    km_path = workspace / "knowledge" / "knowledge-map.yaml"
    out_path = workspace / "knowledge" / "domain-summary.yaml"

    if not km_path.exists():
        print(f"Error: {km_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(km_path) as f:
        km = yaml.safe_load(f)

    domains = km.get("domains", [])
    summary = {
        "domain_count": len(domains),
        "domains": [],
    }

    for d in domains:
        summary["domains"].append({
            "id": d["id"],
            "概念": d["概念"],
            "自足性": d.get("自足性", "未标注"),
            "关联域": d.get("关联域", []),
            "知识点数": len(d.get("核心知识点", [])),
            "陷阱数": len(d.get("常见陷阱", [])),
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(summary, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"Generated domain summary ({summary['domain_count']} domains) at {out_path}")


if __name__ == "__main__":
    main()
