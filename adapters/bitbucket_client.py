"""BitbucketClient — a ForgeClient backed by the Bitbucket Cloud REST API (2.0).

Mirrors GitHubClient's field names where Bitbucket can supply them. Bitbucket has
no star count (its social signal is "watchers", which the public API does not
expose as a count), so repo_stars is left unset and such entries simply score on
maintenance + licence rather than adoption — honest about what the forge gives.
It emits repo_last_commit, repo_created, repo_primary_language, repo_languages
(single language only — Bitbucket reports one), has_docs/ci/releases where
detectable, and licence via the LICENSE file. Never raises; HTTP is injected.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from adapters.github_client import detect_spdx_from_text
from core.facts import HarvestedField

_BITBUCKET_HOST_RE = re.compile(
    r"bitbucket\.org[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.IGNORECASE
)
_API_BASE = "https://api.bitbucket.org/2.0"


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def parse_workspace_repo(code_repository: str) -> tuple[str, str] | None:
    m = _BITBUCKET_HOST_RE.search(code_repository.strip())
    return (m.group(1), m.group(2)) if m else None


class BitbucketClient:
    source_id = "bitbucket_api"

    def __init__(self, http: HttpClient, *, token: str | None = None) -> None:
        self._http = http
        self._token = token

    def fetch_repo_signals(self, code_repository: str) -> list[HarvestedField]:
        ws_repo = parse_workspace_repo(code_repository)
        if ws_repo is None:
            return []
        workspace, repo = ws_repo

        payload = self._get_json(f"{_API_BASE}/repositories/{workspace}/{repo}")
        if not isinstance(payload, dict) or "uuid" not in payload:
            return []

        fields = self._fields_from_payload(payload)
        now = datetime.now(timezone.utc)

        def extra(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name, value=None if value is None else str(value),
                source_id=self.source_id, raw_value="", harvested_at=now, fetch_status="ok",
            )

        # Bitbucket reports a single primary language on the repo payload; there is
        # no reliable breakdown endpoint, so repo_languages mirrors that one language.
        lang = payload.get("language")
        if lang:
            fields.append(extra("repo_languages", f"{str(lang).title()}:100"))

        for k, v in self._code_health(workspace, repo, payload).items():
            fields.append(extra(k, v))

        detected = self._detect_license_from_file(workspace, repo, payload)
        if detected:
            fields.append(extra("license", detected))
        return fields

    def _code_health(self, workspace: str, repo: str, payload: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        branch = (payload.get("mainbranch") or {}).get("name") or "main"
        tree = self._get_json(
            f"{_API_BASE}/repositories/{workspace}/{repo}/src/{branch}/?pagelen=100"
        )
        if isinstance(tree, dict) and isinstance(tree.get("values"), list):
            names = {str(v.get("path", "")).lower() for v in tree["values"] if isinstance(v, dict)}
            out["has_tests"] = str(any(p in names for p in ("tests", "test", "spec", "specs")))
            out["has_ci"] = str("bitbucket-pipelines.yml" in names)
            out["has_docs"] = str(
                any(p in names for p in ("docs", "doc", "documentation"))
                or any(p.startswith("readme") for p in names)
            )
        # Bitbucket has no releases concept; tags are the closest, but keep it honest.
        return out

    def _detect_license_from_file(self, workspace: str, repo: str, payload: dict[str, Any]) -> str | None:
        branch = (payload.get("mainbranch") or {}).get("name") or "main"
        for fname in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
            # The src endpoint returns the raw file body (not base64-wrapped JSON).
            raw = self._get_text(
                f"{_API_BASE}/repositories/{workspace}/{repo}/src/{branch}/{fname}"
            )
            if raw:
                spdx = detect_spdx_from_text(raw)
                if spdx:
                    return spdx
        return None

    def _get_json(self, url: str) -> Any:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
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

    def _get_text(self, url: str) -> str | None:
        """Fetch a raw file body from the src endpoint (returns text, not JSON)."""
        headers = {"Accept": "*/*"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = self._http.get(url, headers=headers, timeout=15.0)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        # httpx response: prefer .text; fall back to decoding JSON-ish bodies.
        text = getattr(resp, "text", None)
        if isinstance(text, str) and text:
            return text
        try:
            body = resp.json()
        except Exception:
            return None
        if isinstance(body, dict) and body.get("encoding") == "base64" and body.get("content"):
            try:
                return base64.b64decode(body["content"]).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                return None
        return None

    def _fields_from_payload(self, payload: dict[str, Any]) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        raw = json.dumps(payload, separators=(",", ":"), default=str)

        def field(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name, value=None if value is None else str(value),
                source_id=self.source_id, raw_value=raw, harvested_at=now, fetch_status="ok",
            )

        lang = payload.get("language")
        website = payload.get("website") or None
        return [
            # No star_count on Bitbucket — repo_stars intentionally omitted.
            field("repo_last_commit", payload.get("updated_on")),
            field("repo_created", payload.get("created_on")),
            field("repo_archived", None),
            field("repo_primary_language", str(lang).title() if lang else None),
            field("repo_homepage", website),
            field("license", None),  # from the LICENSE file
            field("description", payload.get("description")),
        ]
