"""MCP edge — exposes the catalog to AI agents as stateless Streamable-HTTP tools.

Each tool is a thin wrapper over a core.catalog function; the MCP layer only
translates tool calls into catalog calls and returns the results. Tools query the
store live (facts carry their source), so an agent gets structured, lineage-aware
data without spending its context window on it.

`create_mcp` takes the store and optional embedder so tests inject fakes;
`get_mcp` wires the real DuckDB store + MiniLM embedder for serving.
"""

from __future__ import annotations

from fastmcp import FastMCP

from core import catalog
from core.catalog import EntryStore, QueryEmbedder


def create_mcp(store: EntryStore, embedder: QueryEmbedder | None = None) -> FastMCP:
    mcp = FastMCP(
        name="MACH catalog",
        instructions=(
            "Machine-Actionable Catalog for Healthcare. Search and inspect curated "
            "open-source healthcare software, models, datasets, and standards. Every "
            "harvested fact carries the source it came from."
        ),
    )

    @mcp.tool
    def list_entries(category: str | None = None) -> list[dict]:
        """List catalog entries, optionally filtered by category
        (software, ai-ml-models, datasets, mcp-servers, data-sources, specs, catalogs)."""
        return [e.model_dump() for e in catalog.list_entries(store, category=category)]

    @mcp.tool
    def get_entry(identifier: str) -> dict | None:
        """Full metadata for one entry: its seed fields plus every harvested fact
        with the source it came from. Returns null if no such entry."""
        entry = catalog.get_entry(store, identifier)
        return entry.model_dump() if entry is not None else None

    @mcp.tool
    def search_catalog(query: str, limit: int = 5, category: str | None = None) -> list[dict]:
        """Semantic search over the catalog. Returns the top matches with a
        similarity score, optionally restricted to a category. Requires search
        to be configured."""
        if embedder is None:
            raise ValueError("Search unavailable: no embedder configured")
        hits = catalog.search(store, embedder, query, limit=limit, category=category)
        return [h.model_dump() for h in hits]

    return mcp


def get_mcp() -> FastMCP:
    from adapters.minilm_embedder import MiniLMEmbedder
    from edges.api.app import _default_store

    return create_mcp(_default_store(), MiniLMEmbedder())
