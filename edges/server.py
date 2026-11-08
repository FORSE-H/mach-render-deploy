"""Combined deploy entrypoint — REST + MCP in one ASGI app, one process.

The REST API (catalog, dataset, search, livez, readyz) is the FastAPI app; the
MCP server is mounted under /mcp as a stateless Streamable-HTTP sub-app. Both
share a single read-only DuckDbStore and one embedder, so the catalog is loaded
once. This is what the container serves.

`app` is the ASGI application an ASGI server (uvicorn) runs:
    uvicorn edges.server:app --host 0.0.0.0 --port 7860
"""

from __future__ import annotations

from adapters.minilm_embedder import MiniLMEmbedder
from edges.api.app import _default_store, create_app
from edges.mcp.server import create_mcp


def build_app():
    """Build the combined REST + MCP ASGI app over a shared store + embedder."""
    store = _default_store()
    embedder = MiniLMEmbedder()

    mcp = create_mcp(store, embedder)
    # FastMCP's http_app already serves at /mcp and carries its own lifespan; the
    # lifespan MUST be passed to the host app at construction or the MCP session
    # manager never starts. Mount at root so its internal /mcp lands at /mcp.
    mcp_app = mcp.http_app(stateless_http=True, transport="http")

    api = create_app(store, embedder, lifespan=mcp_app.lifespan)
    api.mount("/", mcp_app)
    return api


app = build_app()
