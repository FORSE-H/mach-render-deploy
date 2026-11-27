"""PyPIClient — download signals from PyPI (a SignalSource).

Answers 'how many users?' for Python packages via PyPI's public download stats
(pypistats.org, no key). The package name is inferred from the seed; when the
guess isn't a real package, the result is an honest 'not_found' fact rather than
silence, so a consumer can distinguish 'no PyPI package' from 'not checked'.

Ownership check — the guessed name can collide. Many healthcare projects have
generic one-word names (care, uni, esm, ...), and a *different* project may hold
that name on PyPI. Inferring the name from the seed and trusting the stats would
then publish download numbers for the wrong package. So before trusting the
count, we read the PyPI package's own repository URL and require it to match the
entry's code_repository (same forge org/name). On a mismatch the count is for a
different project, so we return 'not_found' with a note instead of a wrong
number. When PyPI lists no repository we cannot verify, so the count is kept but
marked unverified in the note — a known gap, not a silent wrong value.

Only the inferred name is tried here; a curator can later pin an exact package
name (future seed column) without any change to this adapter's contract.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField
from core.seeds import Seed

_STATS_URL = "https://pypistats.org/api/packages/{name}/recent"
_META_URL = "https://pypi.org/pypi/{name}/json"


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def infer_package_name(seed: Seed) -> str:
    """Best-effort PyPI package name from the seed identifier (lowercased)."""
    return seed.identifier.strip().lower()


_GH_RE = re.compile(r"github\.com[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[/#?].*)?$", re.I)


def _github_slug(url: str | None) -> str | None:
    """'https://github.com/Org/Repo.git' -> 'org/repo', else None."""
    if not url:
        return None
    m = _GH_RE.search(url.strip())
    return f"{m.group(1).lower()}/{m.group(2).lower()}" if m else None


def _pypi_repo_urls(info: dict) -> list[str]:
    """Candidate repository/homepage URLs declared by a PyPI package's metadata."""
    urls = list((info.get("project_urls") or {}).values())
    if info.get("home_page"):
        urls.append(info["home_page"])
    return [u for u in urls if isinstance(u, str) and u]


class PyPIClient:
    source_id = "pypi"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        name = infer_package_name(seed)
        now = datetime.now(timezone.utc)

        # Ownership gate: does the PyPI package 'name' actually belong to this
        # entry's repo? A mismatch means the name collided with another project.
        verdict, note = self._verify_owner(name, seed.code_repository)
        if verdict == "mismatch":
            return [self._status_field("pypi_downloads", "not_found", note, now, name)]

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
        raw = json.dumps(
            {"stats": body, "ownership": note}, separators=(",", ":"), default=str
        )
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

    def _verify_owner(self, name: str, code_repository: str | None) -> tuple[str, str]:
        """Compare the PyPI package's declared repo to the entry's repo.

        Returns (verdict, note):
          - 'match'      — the package points back at this entry's repo.
          - 'mismatch'   — the package points at a *different* repo; the name
                           collided, so its stats are not this project's.
          - 'unverified' — can't tell (no entry repo, no repo on PyPI, PyPI not
                           reachable, or a non-GitHub repo); keep the count but
                           say so. Never blocks on uncertainty.
        """
        entry_slug = _github_slug(code_repository)
        if not entry_slug:
            return "unverified", "entry has no GitHub repo to verify against"
        try:
            resp = self._http.get(
                _META_URL.format(name=name),
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
        except Exception:
            return "unverified", "could not reach PyPI to verify package ownership"
        if resp.status_code != 200:
            # 404 here means the name isn't a real PyPI package; the stats call
            # that follows will return not_found on its own.
            return "unverified", "PyPI metadata unavailable to verify ownership"
        body = resp.json()
        info = body.get("info", {}) if isinstance(body, dict) else {}
        pkg_slugs = {_github_slug(u) for u in _pypi_repo_urls(info)}
        pkg_slugs.discard(None)
        if not pkg_slugs:
            return "unverified", "PyPI package lists no repository to verify against"
        if entry_slug in pkg_slugs:
            return "match", f"PyPI package repo matches entry repo ({entry_slug})"
        return (
            "mismatch",
            f"PyPI package '{name}' repo {sorted(pkg_slugs)} does not match "
            f"entry repo {entry_slug}; likely a name collision",
        )

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
