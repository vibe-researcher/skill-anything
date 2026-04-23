#!/usr/bin/env python3
"""PostToolUse hook for Bash tool.

Selectively records meaningful Bash operations into events.jsonl:
  * `git tag skill-v*`  → snapshot_created event (enforces P10 git discipline)
  * `git commit` on workspace subtree → snapshot_created event

Never blocks; always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


TAG_RE = re.compile(r"\bgit\s+tag\s+(skill-v\S+)")
COMMIT_RE = re.compile(r"\bgit\s+commit\b")


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
        return 0

    if data.get("tool_name") != "Bash":
        return 0

    command = (data.get("tool_input", {}) or {}).get("command", "")
    if not command:
        return 0

    ws = _workspace()
    if ws is None:
        return 0

    event_type = ""
    summary = ""
    m = TAG_RE.search(command)
    if m:
        event_type = "snapshot_created"
        summary = f"git tag {m.group(1)}"
    elif COMMIT_RE.search(command) and "workspace" in command:
        event_type = "snapshot_created"
        summary = "workspace commit"

    if not event_type:
        return 0

    cmd = [
        "python3",
        str(_project_dir() / "scripts" / "state_manager.py"),
        str(ws),
        "append-event",
        "--event-type", event_type,
        "--agent", "orchestrator",
        "--summary", summary[:120],
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
