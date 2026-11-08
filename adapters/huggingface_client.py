"""HuggingFaceClient — download signals from the Hugging Face Hub (a SignalSource).

Answers 'how many users?' for models/datasets via the HF API (no key for public
repos). HF ids are usually 'org/name'; this adapter only attempts a lookup when
the seed's url/repo points at huggingface.co, from which it extracts the id —
otherwise it reports 'not_found' rather than guessing wildly. As with PyPI, an
unresolved lookup yields an honest status fact, not silence.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from core.facts import HarvestedField
from core.seeds import Seed

_HF_ID_RE = re.compile(r"huggingface\.co/(?:models/|datasets/)?([^/\s]+/[^/\s#?]+)", re.I)
_API = "https://huggingface.co/api/models/{hf_id}"


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def infer_hf_id(seed: Seed) -> str | None:
    """Extract an 'org/name' HF id from the seed's url or code_repository."""
    for candidate in (seed.url, seed.code_repository or ""):
        m = _HF_ID_RE.search(candidate)
        if m:
            return m.group(1)
    return None


class HuggingFaceClient:
    source_id = "huggingface"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        hf_id = infer_hf_id(seed)
        if hf_id is None:
            return [self._status("hf_downloads", "not_found", "no HF id in seed", now, None)]

        try:
            resp = self._http.get(
                _API.format(hf_id=hf_id),
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
        except Exception as exc:
            return [self._status("hf_downloads", "error", str(exc)[:120], now, hf_id)]

        if resp.status_code == 404:
            return [self._status("hf_downloads", "not_found", "no HF repo", now, hf_id)]
        if resp.status_code == 429:
            return [self._status("hf_downloads", "rate_limited", "429", now, hf_id)]
        if resp.status_code != 200:
            return [self._status("hf_downloads", "error", str(resp.status_code), now, hf_id)]

        body = resp.json()
        downloads = body.get("downloads") if isinstance(body, dict) else None
        raw = json.dumps(body, separators=(",", ":"), default=str)
        return [
            HarvestedField(
                field_name="hf_downloads",
                value=None if downloads is None else str(downloads),
                source_id=self.source_id,
                raw_value=raw,
                harvested_at=now,
                fetch_status="ok",
            )
        ]

    def _status(
        self, field: str, status: str, note: str, now: datetime, hf_id: str | None
    ) -> HarvestedField:
        return HarvestedField(
            field_name=field,
            value=None,
            source_id=self.source_id,
            raw_value=json.dumps({"hf_id": hf_id, "note": note}),
            harvested_at=now,
            fetch_status=status,  # type: ignore[arg-type]
        )
