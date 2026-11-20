"""DiscoveryClient — fetch a repo's own files and extract external identifiers.

This is the thin HTTP half of discovery; the parsing logic lives in core/discovery
(pure, tested without a network). Given a code-repository URL on any supported
forge, it fetches the README, the common package manifests, and CITATION.cff via
each forge's RAW file endpoint (no API token, no base64 — one code path for all
forges), runs the core parsers over them, and returns a `Discovered` set of ids
plus lineage-bearing `discovered_*` facts.

Returning both shapes is deliberate: the facts record what was found (auditable,
stored), and the `Discovered` object is handed in-memory to the registry/HF
clients in the same pass, so a conda/npm/PyPI lookup can fire on a name this
discovered without it ever being typed into the seed.

It never raises: a missing file or unreachable forge yields fewer ids, never an
error that sinks the harvest.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote

from core.discovery import (
    Discovered,
    MANIFEST_PARSERS,
    parse_citation_cff,
    scan_text,
)
from core.facts import HarvestedField

DISCOVERY_SOURCE_ID = "discovery"

# Files worth fetching. READMEs and CITATION are scanned as free text; the
# manifest files are parsed by their dedicated parser (see core.discovery).
_README_NAMES = ("README.md", "README.rst", "README.txt", "README")
_CITATION_NAMES = ("CITATION.cff",)
_MANIFEST_NAMES = ("pyproject.toml", "setup.py", "setup.cfg", "package.json", "DESCRIPTION")
# conda recipes usually live under recipe/ or conda.recipe/ — try a couple of spots.
_CONDA_PATHS = ("meta.yaml", "recipe/meta.yaml", "conda.recipe/meta.yaml")

# How to turn a repo URL into a raw-file base, per forge. {branch} is substituted.
_GITHUB_RE = re.compile(r"github\.com[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.I)
_GITLAB_RE = re.compile(r"gitlab\.com[/:]+(.+?)(?:\.git)?/?(?:/-/.*)?$", re.I)
_CODEBERG_RE = re.compile(r"codeberg\.org[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.I)
_BITBUCKET_RE = re.compile(r"bitbucket\.org[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.I)

_BRANCHES = ("HEAD", "main", "master")


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int
    text: str


def _raw_bases(code_repository: str) -> list[str]:
    """Return raw-file base URLs to try (one per candidate branch), or [].

    A base ends with '/', so a filename appends directly. HEAD is tried first
    (GitHub/Codeberg resolve it to the default branch); main/master are fallbacks
    for hosts that don't accept HEAD in a raw path.
    """
    u = code_repository.strip()
    m = _GITHUB_RE.search(u)
    if m:
        owner, repo = m.group(1), m.group(2)
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{b}/" for b in _BRANCHES]
    m = _CODEBERG_RE.search(u)
    if m:
        owner, repo = m.group(1), m.group(2)
        return [f"https://codeberg.org/{owner}/{repo}/raw/branch/{b}/" for b in ("main", "master")]
    m = _GITLAB_RE.search(u)
    if m:
        path = m.group(1).strip("/")
        enc = quote(path, safe="/")
        return [f"https://gitlab.com/{enc}/-/raw/{b}/" for b in ("main", "master")]
    m = _BITBUCKET_RE.search(u)
    if m:
        ws, repo = m.group(1), m.group(2)
        return [f"https://bitbucket.org/{ws}/{repo}/raw/{b}/" for b in ("main", "master")]
    return []


class DiscoveryClient:
    source_id = DISCOVERY_SOURCE_ID

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def discover(self, code_repository: str) -> Discovered:
        """Fetch and scan a repo's files, returning the identifiers found.
        Pure of side effects beyond HTTP GETs; never raises."""
        found = Discovered()
        bases = _raw_bases(code_repository)
        if not bases:
            return found

        # Use the FIRST base that yields a README (that branch is live); fall back
        # across bases only until one works, to avoid hammering every branch.
        base = self._first_live_base(bases)
        if base is None:
            return found

        # READMEs + CITATION → free-text scan.
        for name in _README_NAMES:
            text = self._get_text(base + name)
            if text:
                found.merge(scan_text(text))
                break  # one README is enough
        for name in _CITATION_NAMES:
            text = self._get_text(base + name)
            if text:
                found.merge(parse_citation_cff(text))
                break

        def add(bucket_name: str, value: str | None) -> None:
            if value:
                bucket = getattr(found, bucket_name)
                if value not in bucket:
                    bucket.append(value)

        # Manifests → their dedicated parser (declared name is authoritative).
        for name in _MANIFEST_NAMES:
            entry = MANIFEST_PARSERS.get(name.lower())
            if entry is None:
                continue
            text = self._get_text(base + name)
            if not text:
                continue
            bucket_name, parser = entry
            add(bucket_name, parser(text))  # type: ignore[operator]

        # conda recipe (may be nested under recipe/ or conda.recipe/).
        _, conda_parser = MANIFEST_PARSERS["meta.yaml"]
        for path in _CONDA_PATHS:
            text = self._get_text(base + path)
            if text:
                add("conda", conda_parser(text))  # type: ignore[operator]
                break

        return found

    def fetch_signals_from_discovered(self, found: Discovered) -> list[HarvestedField]:
        """Turn a Discovered set into lineage-bearing `discovered_*` facts.
        One fact per id kind, value = comma-joined ids (or None when none found)."""
        now = datetime.now(timezone.utc)

        def fld(name: str, values: list[str]) -> HarvestedField:
            return HarvestedField(
                field_name=name,
                value=",".join(values) if values else None,
                source_id=self.source_id,
                raw_value=json.dumps({name: values}, separators=(",", ":")),
                harvested_at=now,
                fetch_status="ok" if values else "not_found",
            )

        return [
            fld("discovered_pypi", found.pypi),
            fld("discovered_conda", found.conda),
            fld("discovered_npm", found.npm),
            fld("discovered_cran", found.cran),
            fld("discovered_hf", found.hf),
            fld("discovered_doi", found.doi),
        ]

    # ── internals ────────────────────────────────────────────────────────────

    def _first_live_base(self, bases: list[str]) -> str | None:
        """The first base whose README (any common name) resolves — proving the
        branch exists — so later fetches don't retry dead branches."""
        for base in bases:
            for name in _README_NAMES:
                if self._get_text(base + name) is not None:
                    return base
        return None

    def _get_text(self, url: str) -> str | None:
        """GET a raw file as text, or None on any non-200/error. Never raises."""
        try:
            resp = self._http.get(url, headers={"Accept": "text/plain"}, timeout=15.0)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        text = getattr(resp, "text", None)
        if not isinstance(text, str) or not text:
            return None
        # Guard against huge files (a vendored blob, a data file mis-named).
        return text[:200_000]
