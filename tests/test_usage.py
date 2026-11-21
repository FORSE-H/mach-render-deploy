"""Tests for the MCP usage counter and its /stats surface.

The counter itself (core.usage) is pure and dependency-free, so it is tested
directly and thoroughly. The wiring tests (the /stats route, and that MCP tools
record into a shared counter) import FastAPI / FastMCP and so run in CI where the
deploy deps are installed; locally they skip if those aren't present.
"""

from __future__ import annotations

import json

import pytest

from core.usage import UsageCounter


# ── the counter in isolation ────────────────────────────────────────────────
def test_counts_per_tool():
    u = UsageCounter()
    u.record("get_entry")
    u.record("get_entry")
    u.record("search_catalog")
    snap = u.snapshot()
    assert snap["total_calls"] == 3
    assert snap["tools"]["get_entry"]["calls"] == 2
    assert snap["tools"]["search_catalog"]["calls"] == 1


def test_empty_snapshot_shape():
    snap = UsageCounter().snapshot()
    assert snap["total_calls"] == 0
    assert snap["tools"] == {}
    assert isinstance(snap["since"], str)  # an ISO start timestamp


def test_last_called_recorded():
    u = UsageCounter()
    u.record("list_entries")
    assert u.snapshot()["tools"]["list_entries"]["last_called"] is not None


def test_tools_sorted():
    u = UsageCounter()
    for name in ("search_catalog", "get_entry", "list_entries"):
        u.record(name)
    assert list(u.snapshot()["tools"]) == ["get_entry", "list_entries", "search_catalog"]


# ── persistence ─────────────────────────────────────────────────────────────
def test_persists_and_reloads(tmp_path):
    path = tmp_path / "sub" / "usage.json"  # parent created on write
    u = UsageCounter(path)
    u.record("get_entry")
    u.record("get_entry")
    u.record("explain_scoring")

    # a fresh counter over the same file resumes the tally
    reloaded = UsageCounter(path)
    snap = reloaded.snapshot()
    assert snap["total_calls"] == 3
    assert snap["tools"]["get_entry"]["calls"] == 2
    # and "since" carries over from the persisted file, not reset to now
    assert snap["since"] == u.snapshot()["since"]


def test_written_file_is_valid_json(tmp_path):
    path = tmp_path / "usage.json"
    UsageCounter(path).record("list_entries")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tools"]["list_entries"]["calls"] == 1


def test_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("not json{", encoding="utf-8")
    u = UsageCounter(path)  # must not raise
    assert u.snapshot()["total_calls"] == 0
    u.record("get_entry")  # and recovers to a writable state
    assert json.loads(path.read_text(encoding="utf-8"))["total_calls"] == 1


def test_no_path_means_no_file(tmp_path):
    u = UsageCounter(None)
    u.record("get_entry")  # nothing to write, must not raise
    assert list(tmp_path.iterdir()) == []


# ── wiring: /stats route and MCP recording (need deploy deps) ────────────────
class _FakeStore:
    """Minimal EntryStore for driving catalog.list_entries / get_entry."""

    def __init__(self, seeds):
        self._seeds = seeds

    def list_seeds(self):
        return self._seeds

    def get_seed(self, identifier):
        return next((s for s in self._seeds if s.identifier == identifier), None)

    def facts_with_sources(self, identifier):
        return []

    def history_for(self, identifier, field_name=None):
        return []

    def search_similar(self, query_vector, *, limit, category=None):
        return []


def _seed(identifier="x", category="software"):
    from core.seeds import Seed

    return Seed(identifier=identifier, name=identifier.title(), url="https://e", category=category)


def test_stats_route_reflects_counter():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from edges.api.app import create_app

    usage = UsageCounter()
    usage.record("get_entry")
    app = create_app(_FakeStore([]), embedder=None, usage=usage)
    body = TestClient(app).get("/stats").json()
    assert body["total_calls"] == 1
    assert body["tools"]["get_entry"]["calls"] == 1


def test_stats_route_without_counter_is_empty():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from edges.api.app import create_app

    app = create_app(_FakeStore([]), embedder=None)  # no usage wired
    body = TestClient(app).get("/stats").json()
    assert body == {"since": None, "total_calls": 0, "tools": {}}


def test_mcp_tools_record_into_shared_counter():
    import asyncio

    pytest.importorskip("fastmcp")
    from edges.mcp.server import create_mcp

    usage = UsageCounter()
    store = _FakeStore([_seed("alpha"), _seed("beta")])
    mcp = create_mcp(store, embedder=None, usage=usage)

    # Actually invoke the registered MCP tools; each should record its own name
    # into the shared counter.
    asyncio.run(mcp.call_tool("list_entries", {}))
    asyncio.run(mcp.call_tool("get_entry", {"identifier": "alpha"}))
    asyncio.run(mcp.call_tool("get_entry", {"identifier": "beta"}))
    asyncio.run(mcp.call_tool("get_history", {"identifier": "alpha"}))

    snap = usage.snapshot()
    assert snap["total_calls"] == 4
    assert snap["tools"]["list_entries"]["calls"] == 1
    assert snap["tools"]["get_entry"]["calls"] == 2
    assert snap["tools"]["get_history"]["calls"] == 1
