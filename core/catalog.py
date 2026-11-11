"""Catalog reads — assembling a served entry from seed + harvested facts.

This is the read half of the core: given a store, it returns entries shaped for
consumers. An entry is its seed (the trusted human input) plus its harvested
facts, and every fact carries the source it came from — lineage is part of the
response, not an internal detail, because "where did this value come from?" is
the question MACH exists to answer.

The module depends only on the EntryStore port, so it is served by DuckDB in
production and by an in-memory store in tests, with no framework in sight.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from core.seeds import Seed


class FactWithSource(BaseModel):
    field_name: str
    value: str | None
    source_id: str
    fetch_status: str = "ok"


class Entry(BaseModel):
    identifier: str
    name: str
    url: str
    code_repository: str | None
    category: str
    facts: list[FactWithSource]


class EntrySummary(BaseModel):
    identifier: str
    name: str
    category: str


class SearchHit(BaseModel):
    identifier: str
    name: str
    category: str
    score: float


class ChangeEvent(BaseModel):
    field_name: str
    value: str | None
    previous_value: str | None
    source_id: str
    changed_at: str


class EntryStore(Protocol):
    """Read surface the catalog needs. DuckDbStore implements it."""

    def list_seeds(self) -> list[Seed]: ...

    def get_seed(self, identifier: str) -> Seed | None: ...

    def facts_with_sources(self, identifier: str) -> list[tuple[str, str | None, str, str]]:
        """(field_name, value, source_id, fetch_status) for one entry."""
        ...

    def history_for(
        self, identifier: str, field_name: str | None = None
    ) -> list[tuple[str, str | None, str | None, str, object]]:
        """(field_name, value, previous_value, source_id, changed_at), newest first."""
        ...

    def search_similar(
        self, query_vector: list[float], *, limit: int, category: str | None = None
    ) -> list[tuple[str, str, str, float]]:
        """(identifier, name, category, similarity) for the top matches."""
        ...


class QueryEmbedder(Protocol):
    """Embeds a single query string. The Embedder adapter satisfies this."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def list_entries(store: EntryStore, *, category: str | None = None) -> list[EntrySummary]:
    """All entries as lightweight summaries, optionally filtered by category."""
    return [
        EntrySummary(identifier=s.identifier, name=s.name, category=s.category)
        for s in store.list_seeds()
        if category is None or s.category == category
    ]


def get_entry(store: EntryStore, identifier: str) -> Entry | None:
    """One full entry: seed fields plus harvested facts with their sources."""
    seed = store.get_seed(identifier)
    if seed is None:
        return None
    facts = [
        FactWithSource(field_name=name, value=value, source_id=source, fetch_status=status)
        for name, value, source, status in store.facts_with_sources(identifier)
    ]
    return Entry(
        identifier=seed.identifier,
        name=seed.name,
        url=seed.url,
        code_repository=seed.code_repository,
        category=seed.category,
        facts=facts,
    )


def get_history(store: EntryStore, identifier: str) -> list[ChangeEvent]:
    """The change timeline for an entry: how its harvested values have changed
    over time (newest first), each with its source. Empty if nothing recorded."""
    return [
        ChangeEvent(
            field_name=field,
            value=value,
            previous_value=prev,
            source_id=source,
            changed_at=str(changed_at),
        )
        for field, value, prev, source, changed_at in store.history_for(identifier)
    ]


def search(
    store: EntryStore,
    embedder: QueryEmbedder,
    query: str,
    *,
    limit: int = 5,
    category: str | None = None,
) -> list[SearchHit]:
    """Semantic search: embed the query, rank entries by cosine similarity.

    Returns up to `limit` hits ordered by descending similarity. A blank query
    or an empty catalogue yields no hits.
    """
    if not query.strip():
        return []
    query_vector = embedder.embed([query])[0]
    ranked = store.search_similar(query_vector, limit=limit, category=category)
    return [
        SearchHit(identifier=identifier, name=name, category=category, score=score)
        for identifier, name, category, score in ranked
    ]
