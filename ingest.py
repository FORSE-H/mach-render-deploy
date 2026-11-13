#!/usr/bin/env python3
"""ingest.py — composition root: build a fresh catalogue DuckDB from seeds.

This is the only module that wires concrete adapters (httpx, GitHub, DuckDB)
into the core pipeline. It always builds a brand-new database file and swaps it
into place atomically, so a live reader is never writing the file it serves —
the rebuildable-cache / build-then-swap model in miniature.

`build_catalog` does the work against injected ports and is exercised by tests
with a fake forge client; `main` supplies the real ones.

Usage:
    uv run python ingest.py
    uv run python ingest.py --seeds data/seeds.csv --db data/mach.duckdb
    uv run python ingest.py --token $GITHUB_TOKEN
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from adapters.store import DuckDbStore
from core import catalog
from core.embedding import Embedder, embedding_text
from core.harvest import harvest_seed_from_sources
from core.ports import SignalSource
from core.scoring import health_fields, judgement_fields
from core.seeds import Seed, read_seeds_csv
from core.sources import SOURCES

_ROOT = Path(__file__).parent
_DEFAULT_SEEDS = _ROOT / "data" / "seeds.csv"
_DEFAULT_DB = _ROOT / "data" / "mach.duckdb"


def build_catalog(
    store: DuckDbStore,
    seeds: list[Seed],
    sources: list[SignalSource],
    embedder: Embedder | None = None,
) -> int:
    """Populate a store from seeds: sources, seeds, harvested facts+lineage, and
    (if an embedder is given) one search embedding per entry.

    `sources` is the list of signal sources to harvest from (GitHub, PyPI, HF,
    ...); each seed is enriched by all of them. Returns total fields written.
    """
    store.create_schema()
    store.upsert_sources(SOURCES)
    store.upsert_seeds(seeds)
    fields = sum(harvest_seed_from_sources(seed, sources, store) for seed in seeds)
    score_entries(store)
    if embedder is not None:
        embed_entries(store, embedder)
    store.write_meta(embedder_model=getattr(embedder, "model_name", None))
    return fields


def score_entries(store: DuckDbStore) -> int:
    """Score each entry from its harvested facts and store the judgement as
    lineage-bearing facts. Returns the number of entries scored."""
    scored = 0
    for seed in store.list_seeds():
        entry = catalog.get_entry(store, seed.identifier)
        if entry is None:
            continue
        fields = judgement_fields(entry) + health_fields(entry)
        if fields:
            store.write_fields(seed.identifier, fields)
            scored += 1
    return scored


def embed_entries(store: DuckDbStore, embedder: Embedder) -> int:
    """Build one embedding per entry from its trusted text. Returns count."""
    pairs = [
        (entry, embedding_text(entry))
        for seed in store.list_seeds()
        if (entry := catalog.get_entry(store, seed.identifier)) is not None
    ]
    vectors = embedder.embed([text for _, text in pairs])
    for (entry, text), vector in zip(pairs, vectors):
        store.write_embedding(entry.identifier, text, vector)
    return len(vectors)


def _build_to(
    db_path: Path,
    seeds: list[Seed],
    sources: list[SignalSource],
    embedder: Embedder | None = None,
) -> int:
    """Build into a fresh temp file beside db_path, then swap it into place."""
    tmp_path = db_path.with_suffix(db_path.suffix + ".build")
    tmp_path.unlink(missing_ok=True)

    store = DuckDbStore.open(tmp_path)
    try:
        fields = build_catalog(store, seeds, sources, embedder)
    finally:
        store.close()

    os.replace(tmp_path, db_path)  # atomic swap
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=Path, default=_DEFAULT_SEEDS)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--no-embed", action="store_true", help="Skip embeddings")
    parser.add_argument(
        "--repo-only", action="store_true", help="GitHub only (skip PyPI/HuggingFace)"
    )
    args = parser.parse_args()

    import httpx

    from adapters.github_client import GitHubClient
    from adapters.huggingface_client import HuggingFaceClient
    from adapters.pypi_client import PyPIClient

    embedder = None
    if not args.no_embed:
        from adapters.minilm_embedder import MiniLMEmbedder

        embedder = MiniLMEmbedder()

    from core.harvest import ForgeAsSource

    seeds = read_seeds_csv(args.seeds)
    with httpx.Client(follow_redirects=True) as http:
        sources: list[SignalSource] = [ForgeAsSource(GitHubClient(http, token=args.token))]
        if not args.repo_only:
            sources += [PyPIClient(http), HuggingFaceClient(http)]
        fields = _build_to(args.db, seeds, sources, embedder)

    embed_note = "no embeddings" if args.no_embed else "with embeddings"
    src_note = "github" if args.repo_only else "github+pypi+hf"
    print(f"Built {args.db}: {len(seeds)} seeds, {fields} fields from {src_note} ({embed_note}).")


if __name__ == "__main__":
    main()
