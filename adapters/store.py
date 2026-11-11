"""DuckDbStore — the catalogue's compiled store, the only module importing duckdb.

It builds the schema, loads seeds, and persists harvested fields. Writing a fact
and its provenance is a single operation (`write_fields`): there is no public way
to record a value without recording where it came from, which is what keeps the
lineage guarantee from depending on caller discipline.

A store instance owns one DuckDB connection. By design the writer always builds a
*fresh* file rather than mutating a live one, so the API can later swap to a new
file with no read/write contention.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from core.embedding import EMBEDDING_DIM
from core.facts import HarvestedField
from core.schema import SCHEMA_STATEMENTS, SCHEMA_VERSION
from core.seeds import Seed
from core.sources import MetadataSource


class SchemaVersionError(RuntimeError):
    """A built DB's schema version does not match the running code's."""


class DuckDbStore:
    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    @classmethod
    def open(cls, db_path: str | Path, *, read_only: bool = False) -> "DuckDbStore":
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(duckdb.connect(str(db_path), read_only=read_only))

    @classmethod
    def open_for_serving(cls, db_path: str | Path) -> "DuckDbStore":
        """Open a built DB read-only and verify its schema version, so a stale
        or incompatible cache is rejected loudly instead of served silently."""
        store = cls.open(db_path, read_only=True)
        store.check_version()
        return store

    @classmethod
    def in_memory(cls) -> "DuckDbStore":
        return cls(duckdb.connect(":memory:"))

    def close(self) -> None:
        self._con.close()

    def create_schema(self) -> None:
        for statement in SCHEMA_STATEMENTS:
            self._con.execute(statement)

    # --- build metadata / versioning -----------------------------------------

    def write_meta(self, embedder_model: str | None) -> None:
        """Stamp this build with the current schema version and time. One row."""
        self._con.execute("DELETE FROM catalog_meta")
        self._con.execute(
            "INSERT INTO catalog_meta (schema_version, built_at, embedder_model) VALUES (?, ?, ?)",
            [SCHEMA_VERSION, datetime.now(timezone.utc), embedder_model],
        )

    def read_meta(self) -> dict | None:
        row = self._con.execute(
            "SELECT schema_version, built_at, embedder_model FROM catalog_meta LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {"schema_version": row[0], "built_at": row[1], "embedder_model": row[2]}

    def check_version(self) -> None:
        meta = self.read_meta()
        found = meta["schema_version"] if meta else None
        if found != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"DB schema version {found!r} != code {SCHEMA_VERSION!r}; rebuild with ingest.py"
            )

    def upsert_sources(self, sources: Iterable[MetadataSource]) -> int:
        rows = [
            (
                s.source_id,
                s.name,
                s.base_url,
                s.data_licence,
                s.attribution_required,
                s.attribution_text,
            )
            for s in sources
        ]
        self._con.executemany(
            """
            INSERT OR REPLACE INTO metadata_sources
                (source_id, name, base_url, data_licence,
                 attribution_required, attribution_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return len(rows)

    def upsert_seeds(self, seeds: Iterable[Seed]) -> int:
        rows = [(s.identifier, s.name, s.url, s.code_repository, s.category) for s in seeds]
        self._con.executemany(
            """
            INSERT OR REPLACE INTO seeds
                (identifier, name, url, code_repository, category)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        return len(rows)

    def write_fields(self, identifier: str, fields: Iterable[HarvestedField]) -> int:
        """Persist harvested fields for one entry, each with its provenance.

        All rows for the entry are written in a single transaction so a fact
        never lands without its lineage: on any failure the whole batch rolls
        back, preserving the invariant that every stored fact is traceable.
        """
        fields = list(fields)
        self._con.execute("BEGIN TRANSACTION")
        try:
            for f in fields:
                # Append to history only when the value actually changed (or is new),
                # capturing previous→new with its source and time.
                prev = self._con.execute(
                    "SELECT value FROM entry_facts WHERE identifier = ? AND field_name = ?",
                    [identifier, f.field_name],
                ).fetchone()
                prev_value = prev[0] if prev is not None else None
                if prev is None or prev_value != f.value:
                    self._con.execute(
                        """
                        INSERT OR REPLACE INTO fact_history
                            (identifier, field_name, value, previous_value, source_id, changed_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            identifier,
                            f.field_name,
                            f.value,
                            prev_value,
                            f.source_id,
                            f.harvested_at,
                        ],
                    )
                self._con.execute(
                    """
                    INSERT OR REPLACE INTO entry_facts
                        (identifier, field_name, value, harvested_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [identifier, f.field_name, f.value, f.harvested_at],
                )
                self._con.execute(
                    """
                    INSERT OR REPLACE INTO field_provenance
                        (identifier, field_name, source_id, harvested_at, raw_value, fetch_status)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        identifier,
                        f.field_name,
                        f.source_id,
                        f.harvested_at,
                        f.raw_value,
                        f.fetch_status,
                    ],
                )
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")
        return len(fields)

    def write_embedding(self, identifier: str, embedded_text: str, embedding: list[float]) -> None:
        self._con.execute(
            """
            INSERT OR REPLACE INTO entry_embeddings
                (identifier, embedded_text, embedding)
            VALUES (?, ?, ?)
            """,
            [identifier, embedded_text, embedding],
        )

    def count_embeddings(self) -> int:
        return self._con.execute("SELECT count(*) FROM entry_embeddings").fetchone()[0]

    def search_similar(
        self, query_vector: list[float], *, limit: int, category: str | None = None
    ) -> list[tuple[str, str, str, float]]:
        """(identifier, name, category, similarity) for the top matches.

        Brute-force cosine over entry_embeddings joined to seeds. Returning the
        display fields here means the caller needs no second lookup. At <=5k rows
        this is sub-millisecond; no vector index is needed.
        """
        sql = f"""
            SELECT s.identifier, s.name, s.category,
                   array_cosine_similarity(e.embedding, ?::FLOAT[{EMBEDDING_DIM}]) AS score
            FROM entry_embeddings e
            JOIN seeds s ON s.identifier = e.identifier
            {{where}}
            ORDER BY score DESC
            LIMIT ?
        """
        params: list = [query_vector]
        where = ""
        if category is not None:
            where = "WHERE s.category = ?"
            params.append(category)
        params.append(limit)
        rows = self._con.execute(sql.format(where=where), params).fetchall()
        return [(i, n, c, float(score)) for i, n, c, score in rows]

    # --- reads (EntryStore port) ---------------------------------------------

    def list_seeds(self) -> list[Seed]:
        rows = self._con.execute(
            """
            SELECT identifier, name, url, code_repository, category
            FROM seeds ORDER BY identifier
            """
        ).fetchall()
        return [
            Seed(identifier=i, name=n, url=u, code_repository=r, category=c)
            for i, n, u, r, c in rows
        ]

    def get_seed(self, identifier: str) -> Seed | None:
        row = self._con.execute(
            """
            SELECT identifier, name, url, code_repository, category
            FROM seeds WHERE identifier = ?
            """,
            [identifier],
        ).fetchone()
        if row is None:
            return None
        i, n, u, r, c = row
        return Seed(identifier=i, name=n, url=u, code_repository=r, category=c)

    def facts_with_sources(self, identifier: str) -> list[tuple[str, str | None, str, str]]:
        """(field_name, value, source_id, fetch_status) per fact, joined to its
        provenance."""
        return self._con.execute(
            """
            SELECT f.field_name, f.value, p.source_id, p.fetch_status
            FROM entry_facts f
            JOIN field_provenance p
              ON f.identifier = p.identifier AND f.field_name = p.field_name
            WHERE f.identifier = ?
            ORDER BY f.field_name
            """,
            [identifier],
        ).fetchall()

    def history_for(
        self, identifier: str, field_name: str | None = None
    ) -> list[tuple[str, str | None, str | None, str, object]]:
        """Change history for an entry: (field_name, value, previous_value,
        source_id, changed_at) per recorded change, newest first. Optionally
        restricted to one field."""
        sql = """
            SELECT field_name, value, previous_value, source_id, changed_at
            FROM fact_history
            WHERE identifier = ?
            {field}
            ORDER BY changed_at DESC, field_name
        """
        params: list = [identifier]
        field_clause = ""
        if field_name is not None:
            field_clause = "AND field_name = ?"
            params.append(field_name)
        return self._con.execute(sql.format(field=field_clause), params).fetchall()

    # --- convenience reads (tests) -------------------------------------------

    def facts_for(self, identifier: str) -> dict[str, str | None]:
        rows = self._con.execute(
            "SELECT field_name, value FROM entry_facts WHERE identifier = ?",
            [identifier],
        ).fetchall()
        return {name: value for name, value in rows}

    def provenance_for(self, identifier: str) -> list[tuple[str, str]]:
        return self._con.execute(
            "SELECT field_name, source_id FROM field_provenance WHERE identifier = ?",
            [identifier],
        ).fetchall()
