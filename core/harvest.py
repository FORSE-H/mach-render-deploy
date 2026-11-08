"""harvest — turn one seed into stored, lineage-bearing facts.

The application action: given a seed and one or more signal sources, fetch each
source's signals and persist them together. It coordinates the ports (SignalSource,
ForgeClient) and the sink without knowing they are GitHub/PyPI/HF and DuckDB — all
injected, so this orchestration is tested with fakes and never touches the network
or a database.

Facts from all sources for one seed are written in a single sink call, so an entry's
signals land atomically (the sink's transaction preserves the no-fact-without-lineage
invariant). A source that finds nothing contributes nothing (or an honest
'not_found' fact); it never fails the whole harvest.
"""

from __future__ import annotations

from typing import Protocol

from core.facts import HarvestedField
from core.ports import ForgeClient, SignalSource
from core.seeds import Seed


class FactSink(Protocol):
    """Whatever persists harvested fields for an entry (the store implements it)."""

    def write_fields(self, identifier: str, fields: list[HarvestedField]) -> int: ...


def harvest_seed(seed: Seed, forge: ForgeClient, sink: FactSink) -> int:
    """Harvest GitHub repo signals for one seed. Returns fields written.

    Kept for the common repo-only case; wraps the forge as a SignalSource.
    """
    return harvest_seed_from_sources(seed, [ForgeAsSource(forge)], sink)


def harvest_seed_from_sources(seed: Seed, sources: list[SignalSource], sink: FactSink) -> int:
    """Harvest one seed across all sources and persist the combined facts.

    Returns the number of fields written. Each source runs independently; their
    facts are concatenated and written in one sink call.
    """
    fields: list[HarvestedField] = []
    for source in sources:
        fields.extend(source.fetch_signals(seed))
    if not fields:
        return 0
    return sink.write_fields(seed.identifier, fields)


class ForgeAsSource:
    """Adapts a ForgeClient (addressed by repo URL) to the SignalSource shape."""

    def __init__(self, forge: ForgeClient) -> None:
        self._forge = forge
        self.source_id = forge.source_id

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        if not seed.code_repository:
            return []
        return self._forge.fetch_repo_signals(seed.code_repository)
