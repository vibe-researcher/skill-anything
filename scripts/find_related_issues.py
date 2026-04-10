#!/usr/bin/env python3
"""Fetch high-value GitHub issues and produce a compact summary.

Usage:
    python find_related_issues.py <repo-path> [--keywords kw1,kw2] [--limit N] [--output <path>]

Requires `gh` CLI to be installed and authenticated.
Gracefully degrades if gh is unavailable or repo is not on GitHub.
Output: YAML with issue titles, labels, and top-comment excerpts.
"""
import sys
import os
import re
import subprocess
import json
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def run_gh(args, cwd=None):
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=30, cwd=cwd,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def list_issues(repo_path, limit=20):
    out = run_gh(
        ["issue", "list", "-L", str(limit), "--sort", "comments",
         "--state", "all", "--json", "number,title,labels,comments,state,url"],
        cwd=repo_path,
    )
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def view_issue(repo_path, number):
    out = run_gh(["issue", "view", str(number), "--json", "title,body,comments"], cwd=repo_path)
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    body = (data.get("body") or "")[:1500]
    comments = data.get("comments", [])
    top_comments = []
    for c in comments[:3]:
        text = (c.get("body") or "")[:500]
        top_comments.append(text)

    return {
        "title": data.get("title", ""),
        "body_excerpt": body,
        "top_comments": top_comments,
    }


def score_relevance(issue, keywords):
    if not keywords:
        return issue.get("comments", 0)
    text = (issue.get("title", "") + " " + " ".join(
        l.get("name", "") for l in issue.get("labels", [])
    )).lower()
    kw_hits = sum(1 for kw in keywords if kw.lower() in text)
    return kw_hits * 100 + issue.get("comments", 0)


def main():
    if len(sys.argv) < 2:
        print("Usage: python find_related_issues.py <repo-path> [--keywords kw1,kw2] [--limit N] [--output <path>]",
              file=sys.stderr)
        sys.exit(1)

    repo_path = sys.argv[1]
    keywords = []
    detail_limit = 5
    output_path = None

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--keywords" and i + 1 < len(args):
            keywords = [k.strip() for k in args[i + 1].split(",") if k.strip()]
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            detail_limit = int(args[i + 1])
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        else:
            i += 1

    issues = list_issues(repo_path)
    if not issues:
        summary = {"status": "no_issues", "note": "gh CLI unavailable or no issues found"}
        _write_output(summary, output_path)
        return

    issues.sort(key=lambda x: score_relevance(x, keywords), reverse=True)
    top_issues = issues[:detail_limit]

    detailed = []
    for issue in top_issues:
        detail = view_issue(repo_path, issue["number"])
        entry = {
            "number": issue["number"],
            "title": issue["title"],
            "state": issue.get("state", ""),
            "comment_count": issue.get("comments", 0),
            "labels": [l.get("name", "") for l in issue.get("labels", [])],
            "url": issue.get("url", ""),
        }
        if detail:
            entry["body_excerpt"] = detail["body_excerpt"]
            if detail["top_comments"]:
                entry["top_comments"] = detail["top_comments"]
        detailed.append(entry)

    overview = [
        {"number": iss["number"], "title": iss["title"],
         "comments": iss.get("comments", 0), "state": iss.get("state", "")}
        for iss in issues[:20]
    ]

    summary = {
        "total_fetched": len(issues),
        "keywords_used": keywords or None,
        "overview": overview,
        "detailed": detailed,
    }

    _write_output(summary, output_path)


def _write_output(data, output_path):
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"issues_found={data.get('total_fetched', 0)} output={output_path}")
    else:
        yaml.dump(data, sys.stdout, allow_unicode=True, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
