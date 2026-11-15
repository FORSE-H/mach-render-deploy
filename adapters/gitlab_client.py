"""GitLabClient — a ForgeClient backed by the GitLab REST API (v4).

Mirrors GitHubClient: it maps a GitLab project URL to the SAME HarvestedField
field names GitHub produces (repo_stars, repo_last_commit, license,
repo_primary_language, repo_languages, has_tests/ci/docs/releases, ...), so the
scorer, repo-health, and site display treat a GitLab-hosted project exactly like
a GitHub one. It never raises on a missing or unreachable project: callers get an
empty list and the harvest continues. HTTP transport is injected for tests.

GitLab differs from GitHub in a few shapes, handled here:
  - a project is addressed by its URL-encoded full path (group/subgroup/repo),
    not owner/repo — GitLab supports nested groups.
  - stars are `star_count`, last activity is `last_activity_at`.
  - `/languages` returns {lang: percent} directly (already percentages).
  - licence detection: the project payload rarely carries SPDX, so we read the
    LICENSE file via the repository-files endpoint and match its text, reusing
    the same signature detector as the GitHub client.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import quote

from adapters.github_client import detect_spdx_from_text
from core.facts import HarvestedField

# Matches gitlab.com project URLs, capturing the full path after the host
# (group/subgroup/.../repo), stopping before a /-/... web suffix, query or anchor.
_GITLAB_HOST_RE = re.compile(
    r"gitlab\.com[/:]+(?P<path>[^?#]+?)(?:\.git)?/?(?:/-/.*)?$", re.IGNORECASE
)
_API_BASE = "https://gitlab.com/api/v4"

_NO_SPDX = {None, "", "NOASSERTION", "NONE"}


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def parse_project_path(code_repository: str) -> str | None:
    """Return the GitLab project's full path (e.g. 'openflexure/openflexure-microscope'),
    or None if the URL is not a gitlab.com project. Trailing '/-/...' web paths and
    '.git' suffixes are stripped so the path addresses the project itself."""
    match = _GITLAB_HOST_RE.search(code_repository.strip())
    if not match:
        return None
    path = match.group("path").strip("/")
    # Guard against a bare host or an obviously non-project path.
    if not path or "/" not in path:
        return None
    return path


class GitLabClient:
    source_id = "gitlab_api"

    def __init__(self, http: HttpClient, *, token: str | None = None) -> None:
        self._http = http
        self._token = token

    def fetch_repo_signals(self, code_repository: str) -> list[HarvestedField]:
        path = parse_project_path(code_repository)
        if path is None:
            return []
        enc = quote(path, safe="")  # URL-encode the whole path incl. slashes

        payload = self._get_json(f"{_API_BASE}/projects/{enc}")
        if not isinstance(payload, dict) or "id" not in payload:
            return []

        fields = self._fields_from_payload(payload)
        now = datetime.now(timezone.utc)

        def extra(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name, value=None if value is None else str(value),
                source_id=self.source_id, raw_value="", harvested_at=now, fetch_status="ok",
            )

        # Language breakdown (GitLab returns {lang: percent} already).
        langs = self._language_breakdown(enc)
        if langs is not None:
            fields.append(extra("repo_languages", langs))
            # Primary language = the biggest share, if the payload didn't name one.
            if not any(f.field_name == "repo_primary_language" and f.value for f in fields):
                top = langs.split(",")[0].split(":")[0] if langs else None
                if top:
                    fields.append(extra("repo_primary_language", top))

        # Code-health proxies (tests/CI/docs/releases).
        health = self._code_health(enc, payload)
        for k, v in health.items():
            fields.append(extra(k, v))

        # Licence: detect from the LICENSE file text when the payload can't name it.
        licence_field = next((f for f in fields if f.field_name == "license"), None)
        if licence_field is None or licence_field.value in _NO_SPDX:
            detected = self._detect_license_from_file(enc, payload)
            if detected:
                if licence_field is not None:
                    fields = [
                        replace(f, value=detected, source_id="gitlab_api+license_text")
                        if f.field_name == "license" else f
                        for f in fields
                    ]
                else:
                    fields.append(HarvestedField(
                        field_name="license", value=detected,
                        source_id="gitlab_api+license_text", raw_value="",
                        harvested_at=now, fetch_status="ok",
                    ))

        return fields

    def _language_breakdown(self, enc_path: str) -> str | None:
        """'Python:82,C++:15' — GitLab's /languages returns {lang: percent} floats.
        None on failure; '' is never returned."""
        body = self._get_json(f"{_API_BASE}/projects/{enc_path}/languages")
        if not isinstance(body, dict) or not body:
            return None
        pairs = sorted(
            ((lang, pct) for lang, pct in body.items() if isinstance(pct, (int, float))),
            key=lambda kv: kv[1], reverse=True,
        )
        out = ",".join(f"{lang}:{round(pct)}" for lang, pct in pairs if round(pct) >= 1)
        return out or None

    def _code_health(self, enc_path: str, payload: dict[str, Any]) -> dict[str, str]:
        """tests / CI / docs / releases, from the repo tree and releases endpoint —
        facts, never a quality verdict. Same signals as the GitHub client."""
        out: dict[str, str] = {}
        tree = self._get_json(f"{_API_BASE}/projects/{enc_path}/repository/tree?per_page=100")
        if isinstance(tree, list):
            names = {str(n.get("name", "")).lower() for n in tree if isinstance(n, dict)}
            out["has_tests"] = str(
                any(p in names for p in ("tests", "test", "spec", "specs"))
                or any(p.startswith("test_") or p.endswith("_test.py") for p in names)
            )
            # GitLab CI is a file at the repo root.
            out["has_ci"] = str(
                ".gitlab-ci.yml" in names or ".github" in names
                or any(p in names for p in (".travis.yml", "azure-pipelines.yml", ".circleci"))
            )
            out["has_docs"] = str(
                any(p in names for p in ("docs", "doc", "documentation"))
                or any(p.startswith("readme") for p in names)
            )
        releases = self._get_json(f"{_API_BASE}/projects/{enc_path}/releases?per_page=1")
        if isinstance(releases, list):
            out["has_releases"] = str(len(releases) > 0)
        return out

    def _detect_license_from_file(self, enc_path: str, payload: dict[str, Any]) -> str | None:
        """Fetch the LICENSE file (on the project's default branch) and match its
        text to a known SPDX id. Returns None if missing or unrecognised."""
        branch = payload.get("default_branch") or "main"
        for fname in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
            enc_file = quote(fname, safe="")
            body = self._get_json(
                f"{_API_BASE}/projects/{enc_path}/repository/files/{enc_file}?ref={quote(branch, safe='')}"
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
        """GET a GitLab API URL, returning parsed JSON or None on any failure.
        The harvest never breaks because one call did."""
        headers = {"Accept": "application/json"}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token
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
                field_name=name,
                value=None if value is None else str(value),
                source_id=self.source_id,
                raw_value=raw,
                harvested_at=now,
                fetch_status="ok",
            )

        # GitLab's licence block (when present) carries an SPDX-ish key.
        lic = payload.get("license") or {}
        spdx = None
        if isinstance(lic, dict):
            spdx = lic.get("key") or lic.get("nickname")
            if spdx:
                spdx = str(spdx).upper() if len(str(spdx)) <= 5 else str(spdx)

        topics = payload.get("topics") or payload.get("tag_list") or []
        topics_val = ",".join(topics) if isinstance(topics, list) and topics else None

        return [
            field("repo_stars", payload.get("star_count")),
            field("repo_forks", payload.get("forks_count")),
            field("repo_last_commit", payload.get("last_activity_at")),
            field("repo_created", payload.get("created_at")),
            field("repo_archived", payload.get("archived")),
            field("repo_open_issues", payload.get("open_issues_count")),
            field("repo_primary_language", None),  # filled from /languages if absent
            field("repo_homepage", payload.get("web_url") or None),
            field("repo_topics", topics_val),
            field("license", spdx),
            field("description", payload.get("description")),
        ]
