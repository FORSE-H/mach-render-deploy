"""OSVClient — known-vulnerability signals from OSV.dev.

OSV.dev is Google's open vulnerability database. Given a discovered package name
and ecosystem (PyPI / npm / crates / etc.), it reports how many known security
advisories exist for that package. This is an honest "heads up" signal, not an
alarm: the count is the package's advisory history, and most will be fixed in
current versions — so it is surfaced as "N known advisories (see OSV)", paired
with the OpenSSF Vulnerabilities check which judges *unfixed* ones.

Takes an identifier directly (like the registry clients), never raises, and
reports an honest status on failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField

OSV_SOURCE_ID = "osv"
_API = "https://api.osv.dev/v1/query"


class HttpClient(Protocol):
    def post(self, url: str, *, json: Any, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class OSVClient:
    source_id = OSV_SOURCE_ID

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch_for_package(self, name: str, ecosystem: str) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        try:
            resp = self._http.post(
                _API,
                json={"package": {"name": name, "ecosystem": ecosystem}},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=15.0,
            )
        except Exception as exc:
            return [self._status("error", str(exc)[:120], now, name)]

        if resp.status_code == 429:
            return [self._status("rate_limited", "429", now, name)]
        if resp.status_code != 200:
            return [self._status("error", str(resp.status_code), now, name)]

        try:
            body = resp.json()
        except Exception:
            return [self._status("error", "bad json", now, name)]

        vulns = body.get("vulns") if isinstance(body, dict) else None
        count = len(vulns) if isinstance(vulns, list) else 0
        ids = [v.get("id") for v in (vulns or [])[:5] if isinstance(v, dict)]
        return [
            HarvestedField(
                field_name="osv_known_vulns",
                value=str(count),
                source_id=self.source_id,
                raw_value=json.dumps({"package": name, "ecosystem": ecosystem, "sample_ids": ids},
                                     separators=(",", ":")),
                harvested_at=now,
                fetch_status="ok",
            )
        ]

    def _status(self, status: str, note: str, now: datetime, name: str) -> HarvestedField:
        return HarvestedField(
            field_name="osv_known_vulns",
            value=None,
            source_id=self.source_id,
            raw_value=json.dumps({"package": name, "note": note}),
            harvested_at=now,
            fetch_status=status,  # type: ignore[arg-type]
        )
