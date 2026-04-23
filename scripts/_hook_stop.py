#!/usr/bin/env python3
"""Stop hook: take a state.json snapshot on session end.

Ensures the workspace has a fresh checkpoint timestamp and a
snapshot_created event so that the next SessionStart can recover cleanly.

Never blocks; always exits 0. Skips if no workspace exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    ws = project_dir / "workspace"
    if not (ws / "state.json").exists():
        return 0
    cmd = [
        "python3",
        str(project_dir / "scripts" / "state_manager.py"),
        str(ws),
        "snapshot",
        "--reason", "session_stop",
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
