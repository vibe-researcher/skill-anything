#!/usr/bin/env python3
"""Extract authoritative tool_use counts for a sub-agent invocation.

Claude Code session logs at ~/.claude/projects/<project>/<session>.jsonl
record every tool_use the main agent makes. Sub-agents spawned via the Agent
tool run either:
    (a) synchronously, producing sidechain entries in the SAME log
        (isSidechain=true, sourceToolAssistantUUID links back to the Agent call)
    (b) asynchronously, producing NO in-log record of their tool uses
        (the toolUseResult just has {agentId, outputFile, status, ...})

Case (a) → this script can count them exactly.
Case (b) → this script returns count=null + reason="async_not_in_log", so the
           Orchestrator knows self-report is the only source (treat with
           suspicion; cross-iter invariant_check catches fabrication patterns).

Subcommands:
    count-by-uuid   Given a tool_use_id of an Agent invocation and the log file,
                    return the sub-agent's tool_use count.
    extract         Given the log and a tool_use_id, write the sub-agent's
                    tool-use records to an output file for archival.

Usage:
    python scripts/subagent_log.py count-by-uuid \\
        --log <session.jsonl> --tool-use-id <toolu_...>

    python scripts/subagent_log.py extract \\
        --log <session.jsonl> --tool-use-id <toolu_...> \\
        --output <path>

    # Convenience: find the newest session log for the current project
    python scripts/subagent_log.py find-latest-log \\
        --project-dir ~/.claude/projects/<...>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _load_log(log_path: Path) -> list[dict]:
    if not log_path.exists():
        raise SystemExit(f"log not found: {log_path}")
    lines = []
    with log_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip bad lines silently
    return lines


def _find_agent_tool_use(lines: list[dict], tool_use_id: str) -> dict | None:
    for d in lines:
        if d.get("type") != "assistant":
            continue
        msg = d.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and
                    block.get("type") == "tool_use" and
                    block.get("id") == tool_use_id):
                return {"entry": d, "block": block}
    return None


def _find_tool_result(lines: list[dict], tool_use_id: str) -> dict | None:
    for d in lines:
        if d.get("type") != "user":
            continue
        msg = d.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and
                    block.get("type") == "tool_result" and
                    block.get("tool_use_id") == tool_use_id):
                return d
    return None


def _count_sidechain_tool_uses(lines: list[dict], source_uuid: str) -> tuple[int, Counter]:
    """Count tool_use entries in sidechain chains originating from source_uuid.

    A sidechain entry has isSidechain=true. Its sourceToolAssistantUUID field
    points back to the assistant message containing the Agent tool_use that
    forked it.
    """
    total = 0
    per_tool: Counter[str] = Counter()
    for d in lines:
        if not d.get("isSidechain"):
            continue
        if d.get("type") != "assistant":
            continue
        if d.get("sourceToolAssistantUUID") != source_uuid:
            continue
        content = d.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                total += 1
                per_tool[block.get("name", "?")] += 1
    return total, per_tool


def cmd_count_by_uuid(args) -> tuple[int, dict]:
    log_path = Path(args.log)
    lines = _load_log(log_path)

    found = _find_agent_tool_use(lines, args.tool_use_id)
    if not found:
        return 1, {
            "ok": False,
            "reason": "tool_use_id_not_found",
            "tool_use_id": args.tool_use_id,
            "log": str(log_path),
        }

    agent_entry = found["entry"]
    agent_block = found["block"]
    source_uuid = agent_entry.get("uuid")

    # The assistant message's UUID is what sidechains reference as sourceToolAssistantUUID
    count, per_tool = _count_sidechain_tool_uses(lines, source_uuid)

    # Consult tool_result for async hint
    tr = _find_tool_result(lines, args.tool_use_id)
    is_async = False
    result_status = ""
    if tr:
        tur = tr.get("toolUseResult", {})
        if isinstance(tur, dict):
            is_async = bool(tur.get("isAsync"))
            result_status = tur.get("status", "")

    if count == 0 and is_async:
        return 0, {
            "ok": True,
            "tool_use_id": args.tool_use_id,
            "source": "not_available",
            "reason": "async_not_in_log",
            "count": None,
            "result_status": result_status,
            "hint": "Async sub-agents do not emit sidechain records; "
                    "tool_use_count_self_report is the only available source, "
                    "treat as anomaly-eligible.",
        }

    return 0, {
        "ok": True,
        "tool_use_id": args.tool_use_id,
        "source": "sidechain_log",
        "count": count,
        "per_tool": dict(per_tool),
        "is_async": is_async,
        "result_status": result_status,
    }


def cmd_extract(args) -> tuple[int, dict]:
    """Dump the sub-agent's tool-use records into an output jsonl."""
    log_path = Path(args.log)
    lines = _load_log(log_path)
    found = _find_agent_tool_use(lines, args.tool_use_id)
    if not found:
        return 1, {"ok": False, "reason": "tool_use_id_not_found"}
    source_uuid = found["entry"].get("uuid")

    records = [
        d for d in lines
        if d.get("isSidechain")
        and d.get("sourceToolAssistantUUID") == source_uuid
    ]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Also count tool uses for convenience
    count = 0
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    count += 1

    return 0, {
        "ok": True,
        "tool_use_id": args.tool_use_id,
        "output_path": str(out_path),
        "records_written": len(records),
        "tool_use_count": count,
    }


def cmd_find_latest_log(args) -> tuple[int, dict]:
    proj = Path(args.project_dir).expanduser()
    if not proj.exists():
        return 1, {"ok": False, "reason": "project_dir_not_found",
                   "path": str(proj)}
    logs = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    if not logs:
        return 1, {"ok": False, "reason": "no_logs"}
    return 0, {"ok": True, "log": str(logs[0])}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="subagent_log")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("count-by-uuid")
    pc.add_argument("--log", required=True)
    pc.add_argument("--tool-use-id", required=True)

    pe = sub.add_parser("extract")
    pe.add_argument("--log", required=True)
    pe.add_argument("--tool-use-id", required=True)
    pe.add_argument("--output", required=True)

    pf = sub.add_parser("find-latest-log")
    pf.add_argument("--project-dir", required=True)

    return p


DISPATCH = {
    "count-by-uuid": cmd_count_by_uuid,
    "extract": cmd_extract,
    "find-latest-log": cmd_find_latest_log,
}


def main() -> int:
    args = build_parser().parse_args()
    try:
        code, result = DISPATCH[args.cmd](args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
