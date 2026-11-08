"""GitHubClient — a ForgeClient backed by the GitHub REST API.

This is the only module that knows GitHub's URL shape, endpoint, and JSON keys.
It maps a code_repository URL to repository signals, each returned as a
HarvestedField carrying the raw API fragment for lineage. It never raises on a
repo that is missing or unreachable: callers get an empty list and the harvest
continues. HTTP transport is injected so tests run without the network.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField

_GITHUB_HOST_RE = re.compile(r"github\.com[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.IGNORECASE)
_API_BASE = "https://api.github.com"


class HttpClient(Protocol):
    """Minimal HTTP surface GitHubClient needs. httpx.Client satisfies it."""

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def parse_owner_repo(code_repository: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL, or None if it is not GitHub."""
    match = _GITHUB_HOST_RE.search(code_repository.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


class GitHubClient:
    source_id = "github_api"

    def __init__(self, http: HttpClient, *, token: str | None = None) -> None:
        self._http = http
        self._token = token

    def fetch_repo_signals(self, code_repository: str) -> list[HarvestedField]:
        owner_repo = parse_owner_repo(code_repository)
        if owner_repo is None:
            return []
        owner, repo = owner_repo

        payload = self._get_repo(owner, repo)
        if payload is None:
            return []

        return self._fields_from_payload(payload)

    def _get_repo(self, owner: str, repo: str) -> dict[str, Any] | None:
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = self._http.get(f"{_API_BASE}/repos/{owner}/{repo}", headers=headers, timeout=15.0)
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body if isinstance(body, dict) else None

    def _fields_from_payload(self, payload: dict[str, Any]) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        raw = json.dumps(payload, separators=(",", ":"), default=str)

        def field(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name,
                value=None if value is None else str(value),
                source_id=self.source_id,
                raw_value=raw,
                harvested_at=now,
            )

        licence = payload.get("license") or {}
        spdx = licence.get("spdx_id") if isinstance(licence, dict) else None

        return [
            field("repo_stars", payload.get("stargazers_count")),
            field("repo_forks", payload.get("forks_count")),
            field("repo_last_commit", payload.get("pushed_at")),
            field("repo_archived", payload.get("archived")),
            field("license", spdx),
            field("description", payload.get("description")),
        ]
