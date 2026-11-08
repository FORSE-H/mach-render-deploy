"""HarvestedField — a single harvested value bound to its provenance.

This is the core data structure of MACH's trust model. Every fact the harvester
produces is a (field_name, value) pair that *carries where it came from*: which
source, when it was fetched, and the raw response fragment it was derived from.

Keeping value and provenance together in one object (rather than as parallel
data the caller must coordinate) is deliberate — it makes it impossible to store
a fact without its lineage. The store splits this into the entry_facts and
field_provenance rows; callers never assemble provenance by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

FetchStatus = Literal["ok", "not_found", "rate_limited", "error"]


@dataclass(frozen=True)
class HarvestedField:
    field_name: str
    """Stable field key, e.g. 'repo_stars', 'license', 'downloads'."""

    value: str | None
    """The harvested value, stringified. None means 'source had no value'
    (a meaningful, recordable fact — distinct from a field never fetched)."""

    source_id: str
    """Which source produced this, e.g. 'github_api'. Maps to metadata_sources."""

    raw_value: str
    """JSON-serialised fragment of the original source response this value came
    from — the auditability payload that answers 'prove it'."""

    harvested_at: datetime
    """When this value was fetched. Set by the harvester at fetch time."""

    fetch_status: FetchStatus = "ok"
    """Outcome of the fetch. 'ok' = real value. 'not_found' = source checked,
    nothing there (an honest negative, e.g. no PyPI package). 'rate_limited' /
    'error' = could not determine, value should not be trusted as absence."""
