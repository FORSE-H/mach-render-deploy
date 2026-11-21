"""Usage counter — a tiny, dependency-free tally of MCP tool calls.

This answers one question the catalog's own data cannot: *is anyone actually
calling the MCP server, and which tools?* It is deliberately minimal — a
per-tool call count plus a last-called timestamp, held in memory and guarded by
a lock so concurrent tool calls don't race.

It is **not** analytics of who or what: no arguments, no inputs, no client
identity, nothing per-request is recorded. Only "tool X was called N times, last
at T". That keeps it privacy-safe (nothing about the caller is stored) and cheap.

Persistence is optional and best-effort. When MACH_ANALYTICS_PATH is set, the
tally is loaded on startup and written back after each call via an atomic
replace, so counts survive within a single container's lifetime. On a platform
with an ephemeral filesystem (e.g. Render's free tier) the file is wiped on
redeploy and the counts restart from zero — that is acceptable for a "usage
pulse" signal, and callers should read the numbers as "since last deploy" unless
the file lives on durable storage.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


class UsageCounter:
    """Thread-safe per-tool call tally with optional file persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._last: dict[str, str] = {}
        self._started = _now_iso()
        self._path = path
        if path is not None:
            self._load()

    # ── recording ──────────────────────────────────────────────────────────
    def record(self, tool: str) -> None:
        """Count one call to `tool`. Persistence failures never raise."""
        with self._lock:
            self._counts[tool] = self._counts.get(tool, 0) + 1
            self._last[tool] = _now_iso()
            snapshot = self._snapshot_locked()
        if self._path is not None:
            _write_atomic(self._path, snapshot)

    # ── reading ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, object]:
        """A JSON-serialisable view of the tally, for a /stats endpoint."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, object]:
        tools = {
            name: {"calls": self._counts[name], "last_called": self._last.get(name)}
            for name in sorted(self._counts)
        }
        return {
            "since": self._started,
            "total_calls": sum(self._counts.values()),
            "tools": tools,
        }

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        assert self._path is not None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # no file yet, or unreadable — start empty
        if not isinstance(data, dict):
            return
        tools = data.get("tools")
        if isinstance(tools, dict):
            for name, rec in tools.items():
                if isinstance(rec, dict) and isinstance(rec.get("calls"), int):
                    self._counts[name] = rec["calls"]
                    last = rec.get("last_called")
                    if isinstance(last, str):
                        self._last[name] = last
        since = data.get("since")
        if isinstance(since, str):
            self._started = since


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    """Write JSON to `path` via a temp file + atomic replace. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass  # persistence is best-effort; a read-only FS must not break serving
