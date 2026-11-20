"""Registry clients — monthly download/usage signals from package registries.

conda-forge, npm, and CRAN, mirroring PyPIClient's shape: given a package NAME
(supplied by discovery, not guessed), fetch a monthly usage figure and return a
lineage-bearing fact, with an honest not_found / rate_limited / error status when
the name doesn't resolve. Each never raises, so a flaky registry never sinks the
harvest.

Unlike the legacy PyPIClient (which infers a name from the seed), these take the
name directly — discovery has already found the authoritative package name — so
they expose `fetch_for_package(name)` rather than `fetch_signals(seed)`. The
composite discovery source calls them in-memory with discovered names.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def _status(source_id: str, field: str, status: str, note: str, name: str) -> HarvestedField:
    return HarvestedField(
        field_name=field,
        value=None,
        source_id=source_id,
        raw_value=json.dumps({"package": name, "note": note}),
        harvested_at=datetime.now(timezone.utc),
        fetch_status=status,  # type: ignore[arg-type]
    )


def _ok(source_id: str, field: str, value: int, body: Any, name: str) -> HarvestedField:
    return HarvestedField(
        field_name=field,
        value=str(value),
        source_id=source_id,
        raw_value=json.dumps(body, separators=(",", ":"), default=str)[:2000],
        harvested_at=datetime.now(timezone.utc),
        fetch_status="ok",
    )


class _BaseRegistry:
    source_id = "registry"
    field_name = "registry_downloads"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def _get(self, url: str) -> "HttpResponse | None":
        try:
            return self._http.get(url, headers={"Accept": "application/json"}, timeout=15.0)
        except Exception:
            return None

    def fetch_for_package(self, name: str) -> list[HarvestedField]:
        raise NotImplementedError


class CondaClient(_BaseRegistry):
    """conda-forge total downloads via anaconda.org's package API."""

    source_id = "conda"
    field_name = "conda_downloads"
    _API = "https://api.anaconda.org/package/conda-forge/{name}"

    def fetch_for_package(self, name: str) -> list[HarvestedField]:
        resp = self._get(self._API.format(name=name))
        if resp is None:
            return [_status(self.source_id, self.field_name, "error", "network", name)]
        if resp.status_code == 404:
            return [_status(self.source_id, self.field_name, "not_found", "no conda-forge package", name)]
        if resp.status_code == 429:
            return [_status(self.source_id, self.field_name, "rate_limited", "429", name)]
        if resp.status_code != 200:
            return [_status(self.source_id, self.field_name, "error", str(resp.status_code), name)]
        try:
            body = resp.json()
        except Exception:
            return [_status(self.source_id, self.field_name, "error", "bad json", name)]
        # Total downloads = sum of per-file ndownloads.
        total = 0
        if isinstance(body, dict) and isinstance(body.get("files"), list):
            total = sum(int(f.get("ndownloads", 0)) for f in body["files"] if isinstance(f, dict))
        return [_ok(self.source_id, self.field_name, total, {"files_total": total}, name)]


class NpmClient(_BaseRegistry):
    """npm last-month downloads via api.npmjs.org."""

    source_id = "npm"
    field_name = "npm_downloads"
    _API = "https://api.npmjs.org/downloads/point/last-month/{name}"

    def fetch_for_package(self, name: str) -> list[HarvestedField]:
        resp = self._get(self._API.format(name=name))
        if resp is None:
            return [_status(self.source_id, self.field_name, "error", "network", name)]
        if resp.status_code == 404:
            return [_status(self.source_id, self.field_name, "not_found", "no npm package", name)]
        if resp.status_code == 429:
            return [_status(self.source_id, self.field_name, "rate_limited", "429", name)]
        if resp.status_code != 200:
            return [_status(self.source_id, self.field_name, "error", str(resp.status_code), name)]
        try:
            body = resp.json()
        except Exception:
            return [_status(self.source_id, self.field_name, "error", "bad json", name)]
        # {"error": "... not found"} comes back as 200 sometimes.
        if isinstance(body, dict) and body.get("error"):
            return [_status(self.source_id, self.field_name, "not_found", "no npm package", name)]
        downloads = body.get("downloads") if isinstance(body, dict) else None
        if downloads is None:
            return [_status(self.source_id, self.field_name, "not_found", "no count", name)]
        return [_ok(self.source_id, self.field_name, int(downloads), body, name)]


class CranClient(_BaseRegistry):
    """CRAN last-month downloads via cranlogs.r-pkg.org."""

    source_id = "cran"
    field_name = "cran_downloads"
    _API = "https://cranlogs.r-pkg.org/downloads/total/last-month/{name}"

    def fetch_for_package(self, name: str) -> list[HarvestedField]:
        resp = self._get(self._API.format(name=name))
        if resp is None:
            return [_status(self.source_id, self.field_name, "error", "network", name)]
        if resp.status_code == 429:
            return [_status(self.source_id, self.field_name, "rate_limited", "429", name)]
        if resp.status_code != 200:
            return [_status(self.source_id, self.field_name, "error", str(resp.status_code), name)]
        try:
            body = resp.json()
        except Exception:
            return [_status(self.source_id, self.field_name, "error", "bad json", name)]
        # Returns [{"downloads": N, "package": name, ...}]; 0 downloads can mean
        # "not on CRAN" or "genuinely zero", so treat 0 as not_found to avoid a
        # misleading zero.
        if isinstance(body, list) and body and isinstance(body[0], dict):
            n = int(body[0].get("downloads", 0))
            if n > 0:
                return [_ok(self.source_id, self.field_name, n, body[0], name)]
        return [_status(self.source_id, self.field_name, "not_found", "no CRAN downloads", name)]
