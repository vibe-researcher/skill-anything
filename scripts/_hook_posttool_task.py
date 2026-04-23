#!/usr/bin/env python3
"""PostToolUse hook for the Task tool.

Records a sub-agent's return into workspace/state/events.jsonl so the audit
trail reflects every spawned agent even if the Orchestrator forgets to log
it manually. Never blocks the tool; always exits 0. Fails silently if no
workspace exists yet (first-run scenarios).

The hook is NOT responsible for OSR validation — the Orchestrator must call
osr_validate.py explicitly per SKILL.md. This hook is the backstop that
guarantees audit completeness.

Claude Code invokes this script via PostToolUse hook. JSON on stdin has:
    {
      "session_id": "...",
      "tool_name": "Task",
      "tool_input": {"subagent_type": "...", "description": "...", ...},
      "tool_response": {...}   (may be present depending on CC version)
    }
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()


def _workspace() -> Path | None:
    ws = _project_dir() / "workspace"
    if (ws / "state.json").exists():
        return ws
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # can't parse, skip silently

    if data.get("tool_name") != "Task":
        return 0

    ws = _workspace()
    if ws is None:
        return 0  # no workspace yet; nothing to log

    tool_input = data.get("tool_input", {}) or {}
    agent_type = tool_input.get("subagent_type", "unknown")
    description = (tool_input.get("description") or "")[:120]

    # Event type: the Task returned, but we can't verify the OSR from here.
    # Use "osr_returned" as the best-matching enum value — Orchestrator validates.
    cmd = [
        "python3",
        str(_project_dir() / "scripts" / "state_manager.py"),
        str(ws),
        "append-event",
        "--event-type", "osr_returned",
        "--agent", str(agent_type),
        "--summary", f"Task returned: {description}"[:120],
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass  # hook must never block the main flow
    return 0


if __name__ == "__main__":
    sys.exit(main())
