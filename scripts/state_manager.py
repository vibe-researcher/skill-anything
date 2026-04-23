#!/usr/bin/env python3
"""Atomic state management for skill-anything v2.

Reads and writes workspace/state.json, workspace/state/iterations/iter-N.json,
and workspace/state/events.jsonl with POSIX file locking and atomic rename so
concurrent processes (Orchestrator + hooks) cannot corrupt each other's writes.

Usage:
    python scripts/state_manager.py <workspace> <subcommand> [args...]

Subcommands:
    init           Create state.json + state/ subdirs + empty events.jsonl
    get            Print state.json (or a dotted-key subtree) to stdout
    set            Atomically update state.json at a dotted-key path
    append-event   Append a line to events.jsonl + update state.last_event_id
    phase-transition   Set state.phase + emit phase_transition event + snapshot
    snapshot       Update state.last_checkpoint_at + emit snapshot_created event
    write-iter     Atomic write of state/iterations/iter-N.json
    read-iter      Print state/iterations/iter-N.json
    append-score   Append to state.scores_history (+ update current_iteration)

All subcommands print a compact JSON result object on stdout for scripted
consumption. Non-zero exit indicates failure; stderr carries human-readable
error text.

Pure stdlib.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------


@contextmanager
def _locked(path: Path, mode: str = "r+"):
    """Acquire an exclusive fcntl lock on path while it is open.

    Creates the file if it does not exist (mode='a+' then reopen).
    Yields the open file handle at offset 0.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    f = path.open(mode)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        yield f
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to path atomically via temp-file-then-rename.

    POSIX rename is atomic within the same filesystem, so a concurrent reader
    either sees the old file or the new one, never a half-written mess.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        # fsync directory so the rename itself is durable
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        # Best-effort cleanup of the tempfile on any error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_jsonl_atomic(path: Path, line_obj: dict) -> None:
    """Append one JSON line. Uses O_APPEND which is atomic for writes smaller
    than PIPE_BUF (typ. 4096); combined with fcntl lock to be safe with larger.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(line_obj, ensure_ascii=False) + "\n"
    encoded = line.encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# State layout helpers
# ---------------------------------------------------------------------------


def _state_path(ws: Path) -> Path:
    return ws / "state.json"


def _events_path(ws: Path) -> Path:
    return ws / "state" / "events.jsonl"


def _iter_path(ws: Path, n: int) -> Path:
    return ws / "state" / "iterations" / f"iter-{n}.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _load_state(ws: Path) -> dict:
    p = _state_path(ws)
    if not p.exists():
        raise FileNotFoundError(f"state.json not found: {p}. Run 'init' first.")
    with _locked(p, "r") as f:
        return json.load(f)


def _save_state(ws: Path, state: dict) -> None:
    _atomic_write_json(_state_path(ws), state)


def _dotted_get(obj: Any, key: str) -> Any:
    """Walk dotted path (e.g., 'open_channels.pending_surprises') on a dict.
    Raises KeyError on missing intermediate keys.
    """
    cur = obj
    if not key:
        return cur
    for part in key.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _dotted_set(obj: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = obj
    for part in parts[:-1]:
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            if part not in cur or not isinstance(cur[part], (dict, list)):
                cur[part] = {}
            cur = cur[part]
    if isinstance(cur, list):
        cur[int(parts[-1])] = value
    else:
        cur[parts[-1]] = value


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_init(args) -> dict:
    ws = Path(args.workspace)
    state_file = _state_path(ws)
    if state_file.exists() and not args.force:
        raise SystemExit(
            f"state.json already exists at {state_file}. Use --force to overwrite."
        )

    (ws / "state" / "iterations").mkdir(parents=True, exist_ok=True)
    (ws / "state" / "surprises").mkdir(parents=True, exist_ok=True)
    (ws / "state" / "rejections").mkdir(parents=True, exist_ok=True)
    (ws / "notes").mkdir(parents=True, exist_ok=True)

    session_id = str(uuid.uuid4())
    state = {
        "schema_version": "2.0.0",
        "session_id": session_id,
        "repo_url": args.repo_url,
        "phase": "init",
        "context_mode": args.context_mode,
        "current_iteration": 0,
        "research_done": False,
        "generation_done": False,
        "scores_history": [],
        "open_channels": {
            "pending_surprises": [],
            "anomalies_count_by_type": {},
            "meta_observations_digest": [],
        },
        "guardrail_flags": [],
        "last_checkpoint_at": _now_iso(),
    }
    _save_state(ws, state)

    # Touch events.jsonl so append-event can flock immediately
    events = _events_path(ws)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.touch()

    # Emit an init event
    ev = _make_event(
        event_type="phase_transition",
        phase="init",
        summary=f"workspace initialized, session={session_id[:8]}",
        payload={"repo_url": args.repo_url, "context_mode": args.context_mode},
    )
    _append_jsonl_atomic(events, ev)
    state["last_event_id"] = ev["event_id"]
    _save_state(ws, state)

    return {"ok": True, "state_path": str(state_file), "session_id": session_id}


def cmd_get(args) -> dict:
    ws = Path(args.workspace)
    state = _load_state(ws)
    if args.key:
        value = _dotted_get(state, args.key)
        return {"ok": True, "key": args.key, "value": value}
    return {"ok": True, "state": state}


def cmd_set(args) -> dict:
    ws = Path(args.workspace)
    with _locked(_state_path(ws), "r") as f:
        state = json.load(f)
    value = json.loads(args.value)
    _dotted_set(state, args.key, value)
    state["last_checkpoint_at"] = _now_iso()
    _save_state(ws, state)
    return {"ok": True, "key": args.key}


def _make_event(
    event_type: str,
    phase: str = "",
    iter_n: int | None = None,
    agent: str = "",
    ref_path: str = "",
    summary: str = "",
    payload: dict | None = None,
) -> dict:
    ev: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "ts": _now_iso(),
        "phase": phase or "init",
        "event_type": event_type,
        "summary": (summary or "")[:120],
    }
    if iter_n is not None:
        ev["iter"] = iter_n
    if agent:
        ev["agent"] = agent
    if ref_path:
        ev["ref_path"] = ref_path
    if payload:
        ev["payload"] = payload
    return ev


def cmd_append_event(args) -> dict:
    ws = Path(args.workspace)
    payload = json.loads(args.payload) if args.payload else None
    # Load state to fill phase/iter defaults
    state = _load_state(ws)
    phase = args.phase or state["phase"]
    iter_n = args.iter if args.iter is not None else state["current_iteration"]
    ev = _make_event(
        event_type=args.event_type,
        phase=phase,
        iter_n=iter_n,
        agent=args.agent,
        ref_path=args.ref,
        summary=args.summary,
        payload=payload,
    )
    _append_jsonl_atomic(_events_path(ws), ev)
    state["last_event_id"] = ev["event_id"]
    state["last_checkpoint_at"] = ev["ts"]
    _save_state(ws, state)
    return {"ok": True, "event_id": ev["event_id"]}


def cmd_phase_transition(args) -> dict:
    ws = Path(args.workspace)
    state = _load_state(ws)
    old = state["phase"]
    state["phase"] = args.to
    state["last_checkpoint_at"] = _now_iso()
    _save_state(ws, state)
    ev = _make_event(
        event_type="phase_transition",
        phase=args.to,
        iter_n=state["current_iteration"],
        summary=f"{old} -> {args.to}",
        payload={"from": old, "to": args.to},
    )
    _append_jsonl_atomic(_events_path(ws), ev)
    state["last_event_id"] = ev["event_id"]
    _save_state(ws, state)
    return {"ok": True, "phase": args.to, "event_id": ev["event_id"]}


def cmd_snapshot(args) -> dict:
    ws = Path(args.workspace)
    state = _load_state(ws)
    state["last_checkpoint_at"] = _now_iso()
    _save_state(ws, state)
    ev = _make_event(
        event_type="snapshot_created",
        phase=state["phase"],
        iter_n=state["current_iteration"],
        summary=f"snapshot reason={args.reason or 'manual'}",
    )
    _append_jsonl_atomic(_events_path(ws), ev)
    state["last_event_id"] = ev["event_id"]
    _save_state(ws, state)
    return {"ok": True, "ts": state["last_checkpoint_at"], "event_id": ev["event_id"]}


def cmd_write_iter(args) -> dict:
    ws = Path(args.workspace)
    data = json.loads(Path(args.data).read_text()) if args.data_from_file else json.loads(args.data)
    path = _iter_path(ws, args.iter)
    _atomic_write_json(path, data)
    # Emit an event so the audit trail shows the iter file was written
    state = _load_state(ws)
    ev = _make_event(
        event_type="snapshot_created",
        phase=state["phase"],
        iter_n=args.iter,
        ref_path=str(path),
        summary=f"iter-{args.iter}.json written",
    )
    _append_jsonl_atomic(_events_path(ws), ev)
    state["last_event_id"] = ev["event_id"]
    _save_state(ws, state)
    return {"ok": True, "path": str(path), "event_id": ev["event_id"]}


def cmd_read_iter(args) -> dict:
    ws = Path(args.workspace)
    path = _iter_path(ws, args.iter)
    if not path.exists():
        raise SystemExit(f"iter-{args.iter}.json not found: {path}")
    data = json.loads(path.read_text())
    return {"ok": True, "iter": args.iter, "data": data}


def cmd_append_score(args) -> dict:
    ws = Path(args.workspace)
    with _locked(_state_path(ws), "r") as f:
        state = json.load(f)
    entry = {"iter": args.iter, "composite": args.composite}
    if args.delta is not None:
        entry["delta"] = args.delta
    if args.cost is not None:
        entry["cost_usd"] = args.cost
    state["scores_history"].append(entry)
    state["current_iteration"] = max(state.get("current_iteration", 0), args.iter)
    state["last_checkpoint_at"] = _now_iso()
    # Emit an event — score appends are meaningful state changes for audit
    ev = _make_event(
        event_type="snapshot_created",
        phase=state["phase"],
        iter_n=args.iter,
        summary=f"iter-{args.iter} composite={args.composite:.4f}",
        payload=entry,
    )
    _append_jsonl_atomic(_events_path(ws), ev)
    state["last_event_id"] = ev["event_id"]
    _save_state(ws, state)
    return {"ok": True, "scores_history_len": len(state["scores_history"]),
            "event_id": ev["event_id"]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="state_manager")
    p.add_argument("workspace", type=str)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init")
    pi.add_argument("--repo-url", required=True)
    pi.add_argument("--context-mode", choices=["minimal", "rich"], default="minimal")
    pi.add_argument("--force", action="store_true")

    pg = sub.add_parser("get")
    pg.add_argument("--key", default="")

    ps = sub.add_parser("set")
    ps.add_argument("--key", required=True)
    ps.add_argument("--value", required=True, help="JSON-encoded value")

    pe = sub.add_parser("append-event")
    pe.add_argument("--event-type", required=True)
    pe.add_argument("--phase", default="")
    pe.add_argument("--iter", type=int, default=None)
    pe.add_argument("--agent", default="")
    pe.add_argument("--ref", default="")
    pe.add_argument("--summary", default="")
    pe.add_argument("--payload", default="", help="JSON-encoded payload")

    pt = sub.add_parser("phase-transition")
    pt.add_argument("--to", required=True, choices=[
        "init", "research", "generate", "iterate", "done", "aborted"
    ])

    pn = sub.add_parser("snapshot")
    pn.add_argument("--reason", default="")

    pw = sub.add_parser("write-iter")
    pw.add_argument("--iter", type=int, required=True)
    pw.add_argument("--data", required=True, help="JSON-encoded data, or path if --data-from-file")
    pw.add_argument("--data-from-file", action="store_true")

    pr = sub.add_parser("read-iter")
    pr.add_argument("--iter", type=int, required=True)

    pas = sub.add_parser("append-score")
    pas.add_argument("--iter", type=int, required=True)
    pas.add_argument("--composite", type=float, required=True)
    pas.add_argument("--delta", type=float, default=None)
    pas.add_argument("--cost", type=float, default=None)

    return p


DISPATCH = {
    "init": cmd_init,
    "get": cmd_get,
    "set": cmd_set,
    "append-event": cmd_append_event,
    "phase-transition": cmd_phase_transition,
    "snapshot": cmd_snapshot,
    "write-iter": cmd_write_iter,
    "read-iter": cmd_read_iter,
    "append-score": cmd_append_score,
}


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = DISPATCH[args.cmd](args)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
              file=sys.stdout)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
