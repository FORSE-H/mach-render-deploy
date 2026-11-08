"""v1 schema — the tables the harvest/serve slice needs.

The schema is the compiled shape of a *rebuildable cache*: it is dropped and
recreated from the JSON-LD seeds plus harvested signals, never the system of
record. Sized for <=5,000 entries, so it favours flexibility (a tall facts
table, no per-category columns) over query micro-optimisation that scale would
demand but this catalogue never will.

Tables:
  seeds            trusted human input (5 fields), fed from the JSON-LD files
  metadata_sources registry of authoritative sources + licence/attribution
  entry_facts      harvested values, one row per (identifier, field_name)
  field_provenance lineage, 1:1 with entry_facts: which source, when, raw value
  entry_embeddings one search vector per entry
  catalog_meta     one row describing this build: schema version, time, embedder

entry_facts and field_provenance share the (identifier, field_name) key so every
fact maps to exactly one piece of provenance. New fields need no migration —
they are new rows, not new columns.

SCHEMA_VERSION is derived automatically from the DDL below (a short hash), so it
changes exactly when the table shapes change — nothing to bump by hand. A store
opens a built DB only if its recorded version matches, so a stale or
incompatible cache fails loudly at boot rather than serving wrong data silently.
"""

from __future__ import annotations

import hashlib

from core.embedding import EMBEDDING_DIM

# Ordered: referenced tables before referencing ones.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS seeds (
        identifier      VARCHAR PRIMARY KEY,
        name            VARCHAR NOT NULL,
        url             VARCHAR NOT NULL,
        code_repository VARCHAR,
        category        VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metadata_sources (
        source_id            VARCHAR PRIMARY KEY,
        name                 VARCHAR NOT NULL,
        base_url             VARCHAR,
        data_licence         VARCHAR,
        attribution_required BOOLEAN NOT NULL DEFAULT FALSE,
        attribution_text     VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entry_facts (
        identifier   VARCHAR NOT NULL,
        field_name   VARCHAR NOT NULL,
        value        VARCHAR,
        harvested_at TIMESTAMP NOT NULL,
        PRIMARY KEY (identifier, field_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS field_provenance (
        identifier   VARCHAR NOT NULL,
        field_name   VARCHAR NOT NULL,
        source_id    VARCHAR NOT NULL,
        harvested_at TIMESTAMP NOT NULL,
        raw_value    VARCHAR,
        fetch_status VARCHAR NOT NULL DEFAULT 'ok',
        PRIMARY KEY (identifier, field_name, harvested_at)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS entry_embeddings (
        identifier    VARCHAR PRIMARY KEY,
        embedded_text VARCHAR NOT NULL,
        embedding     FLOAT[{EMBEDDING_DIM}] NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_meta (
        schema_version VARCHAR NOT NULL,
        built_at       TIMESTAMP NOT NULL,
        embedder_model VARCHAR
    )
    """,
)


def _compute_version() -> str:
    """Short, stable hash of the DDL — changes iff a table shape changes."""
    joined = "".join(" ".join(stmt.split()) for stmt in SCHEMA_STATEMENTS)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]


# Auto-derived: no manual bump. Compare on store open to reject stale caches.
SCHEMA_VERSION = _compute_version()
