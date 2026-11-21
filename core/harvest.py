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


class MultiForgeSource:
    """Routes a seed's repo URL to whichever forge client recognises its host.

    Each forge client returns [] for a URL it doesn't handle, so this just tries
    them in order and returns the first non-empty result. One source in the
    pipeline covers GitHub, GitLab, Codeberg and Bitbucket; a repo on none of them
    simply yields no repo facts (the entry stays not-auto-scored), exactly as a
    single-forge setup did for a non-GitHub URL.
    """

    source_id = "multi_forge"

    def __init__(self, forges: list[ForgeClient]) -> None:
        self._forges = forges

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        if not seed.code_repository:
            return []
        for forge in self._forges:
            fields = forge.fetch_repo_signals(seed.code_repository)
            if fields:
                return fields
        return []


class DiscoveryEnrichedSource:
    """Discovery + the registry/citation sources it feeds, as one pass.

    Harvest sources run independently and don't see each other's facts, so a
    registry lookup can't read discovery's output through the plain SignalSource
    interface. This composite closes that: it runs discovery on the seed's repo,
    emits the `discovered_*` facts for lineage, then hands the discovered package
    names and ids to the registry/HF clients IN-MEMORY and collects their facts
    too — so a conda/npm/CRAN/HF lookup fires on a name discovery found, with no
    hand-typed seed field.

    Clients are injected and optional; a missing one is simply skipped. Each
    client already returns honest status facts and never raises, so this stays
    harvest-safe. A seed's hand-set value (e.g. seed.hf_url) still takes
    precedence: the legacy HF/PyPI sources run separately and their seed-derived
    facts win by being present alongside these.
    """

    source_id = "discovery_enriched"

    def __init__(
        self,
        discovery,            # DiscoveryClient
        *,
        conda=None,           # CondaClient
        npm=None,             # NpmClient
        cran=None,            # CranClient
        openalex=None,        # OpenAlexClient (citations from a discovered DOI)
        openssf=None,         # OpenSSFClient (security posture from the repo URL)
        osv=None,             # OSVClient (known advisories from a discovered package)
        hf=None,              # HuggingFaceClient (optional: enrich discovered HF ids)
    ) -> None:
        self._discovery = discovery
        self._conda = conda
        self._npm = npm
        self._cran = cran
        self._openalex = openalex
        self._openssf = openssf
        self._osv = osv
        self._hf = hf

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        if not seed.code_repository:
            return []
        found = self._discovery.discover(seed.code_repository)
        fields: list[HarvestedField] = list(
            self._discovery.fetch_signals_from_discovered(found)
        )

        # Feed the discovered names to each registry (first name per kind — the
        # declared/most-prominent one; extras are recorded in the discovered_* fact).
        if self._conda and found.conda:
            fields += self._conda.fetch_for_package(found.conda[0])
        if self._npm and found.npm:
            fields += self._npm.fetch_for_package(found.npm[0])
        if self._cran and found.cran:
            fields += self._cran.fetch_for_package(found.cran[0])

        # Citations: the first discovered DOI → OpenAlex (cited_by_count, venue, year).
        if self._openalex and found.doi:
            fields += self._openalex.fetch_for_doi(found.doi[0])

        # Security posture: OpenSSF Scorecard from the repo URL (GitHub only).
        if self._openssf:
            fields += self._openssf.fetch_for_repo(seed.code_repository)

        # Known advisories: OSV for the first discovered package (PyPI > npm > cran).
        if self._osv:
            if found.pypi:
                fields += self._osv.fetch_for_package(found.pypi[0], "PyPI")
            elif found.npm:
                fields += self._osv.fetch_for_package(found.npm[0], "npm")
            elif found.cran:
                fields += self._osv.fetch_for_package(found.cran[0], "CRAN")

        return fields
