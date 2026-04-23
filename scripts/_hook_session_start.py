#!/usr/bin/env python3
"""SessionStart hook: surface the Orchestrator's next_action on startup.

When Claude Code starts a new session in the skill-anything project, this
hook runs preflight.py. If there is a live workspace, the recommended next
action is printed as an additionalContext JSON so the Orchestrator sees
it immediately (Markov recovery).

Never blocks (exit 0 in all normal cases). Emits output only when a
workspace exists — first-run sessions see nothing extra.
"""

from __future__ import annotations

import json
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
        str(project_dir / "scripts" / "preflight.py"),
        str(ws),
        "--no-emit-event",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception:
        return 0

    if r.returncode not in (0, 1):
        return 0

    try:
        pre = json.loads(r.stdout)
    except Exception:
        return 0

    # Emit a tight summary to Claude Code's additionalContext.
    # Claude Code merges stdout of SessionStart hooks into the conversation as
    # an assistant-visible system note. Keep it compact.
    phase = pre.get("state", {}).get("phase") if pre.get("mode") == "resume" else "init"
    iter_n = pre.get("state", {}).get("current_iteration", 0)
    hint = pre.get("recommended_action", "")
    mode = pre.get("mode", "first_run")

    print(
        f"[skill-anything] mode={mode} phase={phase} iter={iter_n}\n"
        f"Next action: {hint}\n"
        f"Run: python3 scripts/preflight.py {ws} for full state."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
