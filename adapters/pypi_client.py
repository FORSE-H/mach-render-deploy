"""PyPIClient — download signals from PyPI (a SignalSource).

Answers 'how many users?' for Python packages via PyPI's public download stats
(pypistats.org, no key). The package name is inferred from the seed; when the
guess isn't a real package, the result is an honest 'not_found' fact rather than
silence, so a consumer can distinguish 'no PyPI package' from 'not checked'.

Only the inferred name is tried here; a curator can later pin an exact package
name (future seed column) without any change to this adapter's contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField
from core.seeds import Seed

_STATS_URL = "https://pypistats.org/api/packages/{name}/recent"


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def infer_package_name(seed: Seed) -> str:
    """Best-effort PyPI package name from the seed identifier (lowercased)."""
    return seed.identifier.strip().lower()


class PyPIClient:
    source_id = "pypi"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        name = infer_package_name(seed)
        now = datetime.now(timezone.utc)
        try:
            resp = self._http.get(
                _STATS_URL.format(name=name),
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
        except Exception as exc:  # network/timeout — unknown, not absence
            return [self._status_field("pypi_downloads", "error", str(exc)[:120], now, name)]

        if resp.status_code == 404:
            return [self._status_field("pypi_downloads", "not_found", "no PyPI package", now, name)]
        if resp.status_code == 429:
            return [self._status_field("pypi_downloads", "rate_limited", "429", now, name)]
        if resp.status_code != 200:
            return [self._status_field("pypi_downloads", "error", str(resp.status_code), now, name)]

        body = resp.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        monthly = data.get("last_month")
        raw = json.dumps(body, separators=(",", ":"), default=str)
        return [
            HarvestedField(
                field_name="pypi_downloads",
                value=None if monthly is None else str(monthly),
                source_id=self.source_id,
                raw_value=raw,
                harvested_at=now,
                fetch_status="ok",
            )
        ]

    def _status_field(
        self, field: str, status: str, note: str, now: datetime, name: str
    ) -> HarvestedField:
        return HarvestedField(
            field_name=field,
            value=None,
            source_id=self.source_id,
            raw_value=json.dumps({"inferred_package": name, "note": note}),
            harvested_at=now,
            fetch_status=status,  # type: ignore[arg-type]
        )
