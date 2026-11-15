"""CodebergClient — a ForgeClient backed by Codeberg's Gitea API (v1).

Codeberg runs Gitea, whose REST API closely follows GitHub's shape, so this
mirrors GitHubClient and emits the SAME HarvestedField field names (repo_stars,
repo_last_commit, license, repo_primary_language, repo_languages,
has_tests/ci/docs/releases, ...). It never raises on a missing or unreachable
repo: callers get an empty list and the harvest continues. HTTP is injected.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol

from adapters.github_client import detect_spdx_from_text
from core.facts import HarvestedField

_CODEBERG_HOST_RE = re.compile(
    r"codeberg\.org[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.IGNORECASE
)
_API_BASE = "https://codeberg.org/api/v1"
_NO_SPDX = {None, "", "NOASSERTION", "NONE"}


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def parse_owner_repo(code_repository: str) -> tuple[str, str] | None:
    m = _CODEBERG_HOST_RE.search(code_repository.strip())
    return (m.group(1), m.group(2)) if m else None


class CodebergClient:
    source_id = "codeberg_api"

    def __init__(self, http: HttpClient, *, token: str | None = None) -> None:
        self._http = http
        self._token = token

    def fetch_repo_signals(self, code_repository: str) -> list[HarvestedField]:
        owner_repo = parse_owner_repo(code_repository)
        if owner_repo is None:
            return []
        owner, repo = owner_repo

        payload = self._get_json(f"{_API_BASE}/repos/{owner}/{repo}")
        if not isinstance(payload, dict) or "id" not in payload:
            return []

        fields = self._fields_from_payload(payload)
        now = datetime.now(timezone.utc)

        def extra(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name, value=None if value is None else str(value),
                source_id=self.source_id, raw_value="", harvested_at=now, fetch_status="ok",
            )

        langs = self._language_breakdown(owner, repo)
        if langs is not None:
            fields.append(extra("repo_languages", langs))
            if not any(f.field_name == "repo_primary_language" and f.value for f in fields):
                top = langs.split(",")[0].split(":")[0] if langs else None
                if top:
                    fields.append(extra("repo_primary_language", top))

        for k, v in self._code_health(owner, repo).items():
            fields.append(extra(k, v))

        # Licence from file text when the payload can't name it (Gitea often can).
        lic_field = next((f for f in fields if f.field_name == "license"), None)
        if lic_field is None or lic_field.value in _NO_SPDX:
            detected = self._detect_license_from_file(owner, repo, payload)
            if detected:
                if lic_field is not None:
                    fields = [
                        replace(f, value=detected, source_id="codeberg_api+license_text")
                        if f.field_name == "license" else f for f in fields
                    ]
                else:
                    fields.append(extra("license", detected))
        return fields

    def _language_breakdown(self, owner: str, repo: str) -> str | None:
        body = self._get_json(f"{_API_BASE}/repos/{owner}/{repo}/languages")
        if not isinstance(body, dict) or not body:
            return None
        total = sum(v for v in body.values() if isinstance(v, int))
        if total <= 0:
            return None
        pairs = sorted(body.items(), key=lambda kv: kv[1], reverse=True)
        out = ",".join(f"{lang}:{round(100 * n / total)}" for lang, n in pairs
                       if round(100 * n / total) >= 1)
        return out or None

    def _code_health(self, owner: str, repo: str) -> dict[str, str]:
        out: dict[str, str] = {}
        tree = self._get_json(f"{_API_BASE}/repos/{owner}/{repo}/contents")
        if isinstance(tree, list):
            names = {str(n.get("name", "")).lower() for n in tree if isinstance(n, dict)}
            out["has_tests"] = str(
                any(p in names for p in ("tests", "test", "spec", "specs"))
                or any(p.startswith("test_") or p.endswith("_test.py") for p in names)
            )
            out["has_ci"] = str(
                ".woodpecker.yml" in names or ".gitea" in names or ".github" in names
                or ".gitlab-ci.yml" in names
                or any(p.startswith(".woodpecker") for p in names)
            )
            out["has_docs"] = str(
                any(p in names for p in ("docs", "doc", "documentation"))
                or any(p.startswith("readme") for p in names)
            )
        releases = self._get_json(f"{_API_BASE}/repos/{owner}/{repo}/releases?limit=1")
        if isinstance(releases, list):
            out["has_releases"] = str(len(releases) > 0)
        return out

    def _detect_license_from_file(self, owner: str, repo: str, payload: dict[str, Any]) -> str | None:
        branch = payload.get("default_branch") or "main"
        for fname in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
            body = self._get_json(
                f"{_API_BASE}/repos/{owner}/{repo}/contents/{fname}?ref={branch}"
            )
            if not isinstance(body, dict):
                continue
            content = body.get("content") or ""
            if body.get("encoding") == "base64" and content:
                try:
                    text = base64.b64decode(content).decode("utf-8", "replace")
                except (ValueError, binascii.Error):
                    continue
            else:
                text = content
            spdx = detect_spdx_from_text(text)
            if spdx:
                return spdx
        return None

    def _get_json(self, url: str) -> Any:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"token {self._token}"
        try:
            resp = self._http.get(url, headers=headers, timeout=15.0)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def _fields_from_payload(self, payload: dict[str, Any]) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        raw = json.dumps(payload, separators=(",", ":"), default=str)

        def field(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name, value=None if value is None else str(value),
                source_id=self.source_id, raw_value=raw, harvested_at=now, fetch_status="ok",
            )

        # Gitea exposes a plain SPDX string in `license` or under `internal_tracker`? No:
        # the repo payload has no licence; it comes from the file. Language is primary.
        return [
            field("repo_stars", payload.get("stars_count")),
            field("repo_forks", payload.get("forks_count")),
            field("repo_last_commit", payload.get("updated_at")),
            field("repo_created", payload.get("created_at")),
            field("repo_archived", payload.get("archived")),
            field("repo_open_issues", payload.get("open_issues_count")),
            field("repo_primary_language", payload.get("language") or None),
            field("repo_homepage", payload.get("website") or None),
            field("repo_topics", None),
            field("license", None),  # filled from the LICENSE file
            field("description", payload.get("description")),
        ]
