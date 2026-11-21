"""OpenSSFClient — security-posture signals from the OpenSSF Scorecard hosted API.

OpenSSF Scorecard (Linux Foundation; Google/Microsoft/GitHub-backed) scores a
repository's security hygiene 0-10 across ~18 checks (branch protection, code
review, CI tests, SAST, pinned dependencies, known vulnerabilities, ...). It is
itself open-source — a good fit for a catalogue of open software, unlike an
opaque commercial scanner.

We do NOT run Scorecard ourselves (heavy, token-hungry). We read its FREE hosted
API, which serves PRE-COMPUTED scores for repos already in its database
(a large share of ours). A covered repo gets a real signal cheaply; an uncovered
one gets an honest not_found; non-GitHub repos are skipped (Scorecard is
GitHub-only).

Honest limits worth surfacing on the site: this measures security *process /
hygiene*, not whether the code is good — a small project can score low on
branch-protection it doesn't need. Never fold it into the Adopt/Assess judgement;
it's a separate axis.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField

OPENSSF_SOURCE_ID = "openssf"
_API = "https://api.securityscorecards.dev/projects/github.com/{owner}/{repo}"
_GITHUB_RE = re.compile(r"github\.com[/:]+([^/]+)/([^/#?]+?)(?:\.git)?/?$", re.I)

# The checks most useful to surface as individual signals (the rest are in raw_value).
_KEY_CHECKS = {"Vulnerabilities", "Maintained", "Code-Review", "Security-Policy", "Dangerous-Workflow"}


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def parse_github(code_repository: str) -> tuple[str, str] | None:
    m = _GITHUB_RE.search((code_repository or "").strip())
    return (m.group(1), m.group(2)) if m else None


class OpenSSFClient:
    source_id = OPENSSF_SOURCE_ID

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch_for_repo(self, code_repository: str) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        gh = parse_github(code_repository)
        if gh is None:
            return [self._status("ssf_score", "not_found", "not a GitHub repo", now, code_repository)]
        owner, repo = gh

        try:
            resp = self._http.get(
                _API.format(owner=owner, repo=repo),
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
        except Exception as exc:
            return [self._status("ssf_score", "error", str(exc)[:120], now, f"{owner}/{repo}")]

        if resp.status_code == 404:
            return [self._status("ssf_score", "not_found", "not in Scorecard DB", now, f"{owner}/{repo}")]
        if resp.status_code == 429:
            return [self._status("ssf_score", "rate_limited", "429", now, f"{owner}/{repo}")]
        if resp.status_code != 200:
            return [self._status("ssf_score", "error", str(resp.status_code), now, f"{owner}/{repo}")]

        try:
            body = resp.json()
        except Exception:
            return [self._status("ssf_score", "error", "bad json", now, f"{owner}/{repo}")]
        if not isinstance(body, dict) or body.get("score") is None:
            return [self._status("ssf_score", "not_found", "no score", now, f"{owner}/{repo}")]

        checks = body.get("checks") or []
        by_check = {c.get("name"): c.get("score") for c in checks if isinstance(c, dict)}
        raw = json.dumps(
            {"score": body.get("score"), "date": body.get("date"), "checks": by_check},
            separators=(",", ":"), default=str,
        )[:3000]

        def fld(name: str, value: Any) -> HarvestedField:
            return HarvestedField(
                field_name=name,
                value=None if value is None else str(value),
                source_id=self.source_id,
                raw_value=raw,
                harvested_at=now,
                fetch_status="ok",
            )

        fields = [fld("ssf_score", body.get("score")), fld("ssf_date", body.get("date"))]
        for name in _KEY_CHECKS:
            if name in by_check:
                key = "ssf_" + name.lower().replace("-", "_")
                fields.append(fld(key, by_check[name]))
        return fields

    def _status(self, field: str, status: str, note: str, now: datetime, who: str) -> HarvestedField:
        return HarvestedField(
            field_name=field,
            value=None,
            source_id=self.source_id,
            raw_value=json.dumps({"repo": who, "note": note}),
            harvested_at=now,
            fetch_status=status,  # type: ignore[arg-type]
        )
