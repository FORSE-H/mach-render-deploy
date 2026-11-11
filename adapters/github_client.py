"""GitHubClient — a ForgeClient backed by the GitHub REST API.

This is the only module that knows GitHub's URL shape, endpoint, and JSON keys.
It maps a code_repository URL to repository signals, each returned as a
HarvestedField carrying the raw API fragment for lineage. It never raises on a
repo that is missing or unreachable: callers get an empty list and the harvest
continues. HTTP transport is injected so tests run without the network.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField

_GITHUB_HOST_RE = re.compile(r"github\.com[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.IGNORECASE)
_API_BASE = "https://api.github.com"

# Values GitHub returns when it cannot name the licence — treat as "detect it ourselves".
_NO_SPDX = {None, "", "NOASSERTION", "NONE"}

# Signature phrases that identify a licence from its text, when GitHub's licensee
# detector declines (usually because the file carries a custom addendum). Ordered
# most-specific first; the first match wins. Kept deliberately small and legible.
_LICENSE_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Apache-2.0", ("Apache License", "Version 2.0")),
    ("GPL-3.0", ("GNU GENERAL PUBLIC LICENSE", "Version 3")),
    ("GPL-2.0", ("GNU GENERAL PUBLIC LICENSE", "Version 2")),
    ("AGPL-3.0", ("GNU AFFERO GENERAL PUBLIC LICENSE", "Version 3")),
    ("LGPL-3.0", ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 3")),
    ("MPL-2.0", ("Mozilla Public License", "Version 2.0")),
    ("BSD-3-Clause", ("Redistributions of source code", "Neither the name")),
    ("BSD-2-Clause", ("Redistributions of source code", "Redistributions in binary form")),
    ("MIT", ("Permission is hereby granted, free of charge",)),
    ("ISC", ("Permission to use, copy, modify, and/or distribute",)),
    ("Unlicense", ("This is free and unencumbered software released into the public domain",)),
    ("CC0-1.0", ("CC0 1.0 Universal",)),
    ("CC-BY-4.0", ("Creative Commons Attribution 4.0",)),
)


def detect_spdx_from_text(text: str) -> str | None:
    """Best-effort SPDX id from licence file text, or None. Case-insensitive and
    whitespace-tolerant so a reformatted or addended file still matches."""
    if not text:
        return None
    norm = " ".join(text.split()).lower()
    for spdx, needles in _LICENSE_SIGNATURES:
        if all(n.lower() in norm for n in needles):
            return spdx
    return None


def _replace_license(field: HarvestedField, spdx: str) -> HarvestedField:
    """Return the licence field with a detected SPDX id, noting the fallback source."""
    return replace(field, value=spdx, source_id="github_api+license_text")


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

        fields = self._fields_from_payload(payload)

        # GitHub's own detector returns NOASSERTION when a LICENSE file carries a
        # custom addendum (common for skill libraries that scope the grant). The
        # file itself is still a standard licence, so when the API can't name it,
        # fall back to detecting the SPDX id from the LICENSE text.
        licence = payload.get("license") or {}
        spdx = licence.get("spdx_id") if isinstance(licence, dict) else None
        if spdx in _NO_SPDX:
            detected = self._detect_license_from_file(owner, repo)
            if detected:
                fields = [
                    f if f.field_name != "license"
                    else _replace_license(f, detected)
                    for f in fields
                ]
        return fields

    def _detect_license_from_file(self, owner: str, repo: str) -> str | None:
        """Fetch the LICENSE file and match its text to a known SPDX id.
        Returns None if the file is missing or unrecognised."""
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = self._http.get(
                f"{_API_BASE}/repos/{owner}/{repo}/license", headers=headers, timeout=15.0
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not isinstance(body, dict):
            return None
        content = body.get("content") or ""
        if body.get("encoding") == "base64" and content:
            try:
                text = base64.b64decode(content).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                return None
        else:
            text = content
        return detect_spdx_from_text(text)

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
