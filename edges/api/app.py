"""FastAPI read edge — a thin adapter over core.catalog.

Routes translate HTTP into catalog calls and return the results; they hold no
business logic. The store is opened read-only for the app's lifetime (the writer
builds a fresh file and swaps, so a read-only handle never contends with it).

`create_app` takes the store (and optional embedder for search) as dependencies
so tests inject fakes; `get_app` opens the DuckDB file named by MACH_DB_PATH and
wires the real embedder.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core import catalog
from core.catalog import EntryStore, QueryEmbedder

# Browser clients (the website's semantic search) need CORS. The API is public
# and read-only, so allowing the site origins plus localhost for dev is safe.
# MACH_CORS_ORIGINS (comma-separated) overrides; "*" allows any origin.
_DEFAULT_ORIGINS = [
    "https://machcatalog.org",
    "https://www.machcatalog.org",
    "https://forse-h.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]


class SearchRequest(BaseModel):
    query: str
    limit: int = 5
    category: str | None = None


def create_app(
    store: EntryStore,
    embedder: QueryEmbedder | None = None,
    *,
    lifespan=None,
) -> FastAPI:
    app = FastAPI(title="MACH catalog API", version="0.1.0", lifespan=lifespan)

    env = os.environ.get("MACH_CORS_ORIGINS")
    origins = ["*"] if env == "*" else (env.split(",") if env else _DEFAULT_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/livez")
    def livez() -> dict[str, str]:
        """Liveness: the process is up and can respond."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, object]:
        """Readiness: the store is reachable and populated, so we can serve."""
        entries = catalog.list_entries(store)
        if not entries:
            raise HTTPException(status_code=503, detail="Not ready: no entries")
        body: dict[str, object] = {
            "status": "ok",
            "entries": len(entries),
            "search": embedder is not None,
        }
        meta = getattr(store, "read_meta", lambda: None)()
        if meta:
            body["schema_version"] = meta["schema_version"]
            body["built_at"] = str(meta["built_at"])
            body["embedder_model"] = meta["embedder_model"]
        return body

    @app.get("/catalog")
    def list_catalog(category: str | None = None) -> list[catalog.EntrySummary]:
        return catalog.list_entries(store, category=category)

    @app.get("/dataset/{identifier}")
    def get_dataset(identifier: str) -> catalog.Entry:
        entry = catalog.get_entry(store, identifier)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"No entry: {identifier}")
        return entry

    @app.post("/search")
    def search(req: SearchRequest) -> list[catalog.SearchHit]:
        if embedder is None:
            raise HTTPException(status_code=503, detail="Search unavailable: no embedder")
        return catalog.search(store, embedder, req.query, limit=req.limit, category=req.category)

    return app


def _default_store() -> EntryStore:
    from adapters.store import DuckDbStore

    db_path = Path(os.environ.get("MACH_DB_PATH", "data/mach.duckdb"))
    if not db_path.exists():
        raise RuntimeError(f"DuckDB not found: {db_path} — run: uv run python ingest.py")
    return DuckDbStore.open_for_serving(db_path)


def get_app() -> FastAPI:
    from adapters.minilm_embedder import MiniLMEmbedder

    return create_app(_default_store(), MiniLMEmbedder())
