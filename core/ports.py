"""Ports — interfaces owned by the core, implemented by adapters.

The core defines what it needs from the outside world (repo signals, storage)
without knowing how those needs are met. Adapters in `adapters/` implement these
protocols against concrete details (GitHub API, DuckDB). Nothing in this module
imports a framework, an HTTP client, or a database driver.
"""

from __future__ import annotations

from typing import Protocol

from core.facts import HarvestedField
from core.seeds import Seed


class SignalSource(Protocol):
    """An authoritative source that enriches a seed with harvested signals.

    This is the extension point: GitHub, PyPI, HuggingFace — and any source
    added tomorrow — implement this one method. Each decides for itself what it
    needs from the seed (a repo URL, an inferred package name, a DOI, ...) and
    returns facts, each carrying its own source + fetch_status for lineage.

    Implementations MUST NOT raise on a missing/unreachable target: they return
    whatever they could determine (possibly a 'not_found' fact, possibly an
    empty list), so one source failing never fails the whole harvest. Adding a
    new source is: implement this + add one metadata_sources row. No schema
    change, because facts are rows in a tall table keyed by source_id.
    """

    source_id: str
    """Identifier of this source in metadata_sources (e.g. 'pypi')."""

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        """Fetch this source's signals for one seed. Empty list if N/A."""
        ...


class ForgeClient(Protocol):
    """A source of repository signals for a code-hosting forge (GitHub first).

    Kept as its own narrower protocol because a forge is addressed by repo URL
    specifically. A ForgeClient is adapted to the SignalSource shape by the
    harvester.
    """

    source_id: str

    def fetch_repo_signals(self, code_repository: str) -> list[HarvestedField]:
        """Fetch repository signals (stars, forks, license, last commit).
        Returns an empty list if the repo cannot be resolved."""
        ...
