#!/usr/bin/env python3
"""export_signals.py — DuckDB → signals.json for the public MACH site.

The single harvester (ingest.py) writes the DuckDB; this reads that same store
and emits one JSON file the static site bakes in at build time. No second
harvest, no live API dependency: the site renders exactly what the API's
/dataset/{id} and /history/{id} would return, but from a file in Git.

For each seed it exports:
  - the stable identity (identifier, name, url, code_repository, category)
  - the harvested facts, each with its source_id and fetch_status (lineage)
  - a few convenience signals pulled out of the facts (stars, forks, last
    commit, licence) so the site needn't re-parse them
  - the deterministic scorer judgement (ring + reason) as the authoritative
    judgement — MACH is rule-based; there is no hand-written judgement here
  - the change history (deviations: previous_value → value) per field

Usage:
    uv run python scripts/export_signals.py
    uv run python scripts/export_signals.py --db data/mach.duckdb --out signals.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from adapters.store import DuckDbStore

_ROOT = Path(__file__).parent.parent
_DEFAULT_DB = _ROOT / "data" / "mach.duckdb"
_DEFAULT_OUT = _ROOT / "data" / "signals.json"

# Facts promoted to top-level convenience signals (the site shows these on cards).
_SIGNAL_FIELDS = ("repo_stars", "repo_forks", "repo_last_commit", "license", "repo_archived")


def _coerce_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def export(store: DuckDbStore) -> dict:
    seeds = store.list_seeds()
    entries = []

    for seed in seeds:
        ident = seed.identifier
        fact_rows = store.facts_with_sources(ident)  # (field, value, source_id, fetch_status)

        facts = [
            {
                "field_name": field,
                "value": value,
                "source_id": source_id,
                "fetch_status": fetch_status,
            }
            for field, value, source_id, fetch_status in fact_rows
        ]
        by_field = {f["field_name"]: f["value"] for f in facts}

        # Convenience signals (parsed once here so the site doesn't have to).
        def _bool(name: str):
            v = by_field.get(name)
            return str(v).lower() == "true" if v is not None else None

        signals = {
            "repo_stars": _coerce_int(by_field.get("repo_stars")),
            "repo_forks": _coerce_int(by_field.get("repo_forks")),
            "repo_last_commit": by_field.get("repo_last_commit"),
            "repo_created": by_field.get("repo_created"),
            "repo_archived": _bool("repo_archived"),
            "repo_open_issues": _coerce_int(by_field.get("repo_open_issues")),
            "license": by_field.get("license"),
            "pypi_downloads": _coerce_int(by_field.get("pypi_downloads")),
            "hf_downloads": _coerce_int(by_field.get("hf_downloads")),
            # Enriched repo signals.
            "primary_language": by_field.get("repo_primary_language"),
            "languages": by_field.get("repo_languages"),   # "Python:82,C++:15"
            "topics": by_field.get("repo_topics"),
            "homepage": by_field.get("repo_homepage"),
            "has_tests": _bool("has_tests"),
            "has_ci": _bool("has_ci"),
            "has_docs": _bool("has_docs"),
            "has_releases": _bool("has_releases"),
        }

        # Rule-based judgement from the scorer (stored as facts by ingest.py).
        judgement = by_field.get("judgement")
        judgement_reason = by_field.get("judgement_reason")
        scored = judgement is not None and judgement_reason is not None

        # Deviation history: (field, value, previous_value, source_id, changed_at)
        history = [
            {
                "field_name": field,
                "value": value,
                "previous_value": previous_value,
                "source_id": source_id,
                "changed_at": _iso(changed_at),
            }
            for field, value, previous_value, source_id, changed_at in store.history_for(ident)
        ]

        entries.append(
            {
                "identifier": ident,
                "name": seed.name,
                "url": seed.url,
                "code_repository": seed.code_repository,
                "category": seed.category,
                "scored": scored,
                "judgement": judgement,
                "judgement_reason": judgement_reason,
                "signals": signals,
                "facts": facts,
                "history": history,
            }
        )

    meta = store.read_meta() or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": len(entries),
        "embedder_model": meta.get("embedder_model"),
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()

    store = DuckDbStore.open(args.db, read_only=True)
    try:
        payload = export(store)
    finally:
        store.close()

    args.out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    scored = sum(1 for e in payload["entries"] if e["scored"])
    print(
        f"Wrote {args.out} — {payload['entry_count']} entries "
        f"({scored} scored, {payload['entry_count'] - scored} not auto-scored)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
