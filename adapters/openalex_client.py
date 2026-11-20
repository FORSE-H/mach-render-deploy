"""OpenAlexClient — citation / academic-impact signals from OpenAlex.

Given a DOI (supplied by discovery as `discovered_doi`), fetches the work's
citation count, publication year, venue, and type from OpenAlex's free, open API
(no key). This answers "is this research-validated?" — MACH's evidence-driven
angle — and turns a bare DOI into a trust signal and a facet.

Like the registry clients it takes an identifier directly (a DOI) rather than
inferring one, and never raises: an unknown DOI or an unreachable API yields an
honest not_found / rate_limited / error status fact, so a flaky lookup never
sinks the harvest.

OpenAlex asks API users to identify themselves with a mailto in the query (the
"polite pool", faster + rate-limit-friendly). We send a generic project contact,
never a personal address.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote

from core.facts import HarvestedField

OPENALEX_SOURCE_ID = "openalex"
# Generic project contact for OpenAlex's polite pool — never a personal address.
_MAILTO = "mach@machcatalog.org"
_API = "https://api.openalex.org/works/doi:{doi}?mailto=" + quote(_MAILTO)


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def _normalise_doi(doi: str) -> str:
    """Strip a leading https://doi.org/ and whitespace, leaving the bare 10.x id."""
    d = (doi or "").strip()
    for p in ("https://doi.org/", "http://doi.org/", "doi.org/", "https://dx.doi.org/"):
        if d.lower().startswith(p):
            d = d[len(p):]
            break
    return d.strip().rstrip("/.")


class OpenAlexClient:
    source_id = OPENALEX_SOURCE_ID

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch_for_doi(self, doi: str) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        norm = _normalise_doi(doi)
        if not norm or not norm.lower().startswith("10."):
            return [self._status("citations", "not_found", "not a DOI", now, norm)]

        try:
            resp = self._http.get(
                _API.format(doi=quote(norm, safe="/")),
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
        except Exception as exc:
            return [self._status("citations", "error", str(exc)[:120], now, norm)]

        if resp.status_code == 404:
            return [self._status("citations", "not_found", "no OpenAlex work", now, norm)]
        if resp.status_code == 429:
            return [self._status("citations", "rate_limited", "429", now, norm)]
        if resp.status_code != 200:
            return [self._status("citations", "error", str(resp.status_code), now, norm)]

        try:
            body = resp.json()
        except Exception:
            return [self._status("citations", "error", "bad json", now, norm)]
        if not isinstance(body, dict) or not body.get("id"):
            return [self._status("citations", "not_found", "empty work", now, norm)]

        raw = json.dumps(
            {k: body.get(k) for k in ("id", "doi", "cited_by_count", "publication_year", "type")},
            separators=(",", ":"), default=str,
        )

        def fld(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name,
                value=None if value in (None, "") else str(value),
                source_id=self.source_id,
                raw_value=raw,
                harvested_at=now,
                fetch_status="ok",
            )

        venue = None
        loc = body.get("primary_location")
        if isinstance(loc, dict) and isinstance(loc.get("source"), dict):
            venue = loc["source"].get("display_name")

        return [
            fld("citations", body.get("cited_by_count")),
            fld("publication_year", body.get("publication_year")),
            fld("openalex_venue", venue),
            fld("openalex_type", body.get("type")),
            fld("openalex_id", body.get("id")),
        ]

    def _status(self, field: str, status: str, note: str, now: datetime, doi: str) -> HarvestedField:
        return HarvestedField(
            field_name=field,
            value=None,
            source_id=self.source_id,
            raw_value=json.dumps({"doi": doi, "note": note}),
            harvested_at=now,
            fetch_status=status,  # type: ignore[arg-type]
        )
