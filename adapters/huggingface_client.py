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
# A '/datasets/' path means the id is a dataset; otherwise treat it as a model.
_HF_DATASET_RE = re.compile(r"huggingface\.co/datasets/", re.I)
_API_MODEL = "https://huggingface.co/api/models/{hf_id}"
_API_DATASET = "https://huggingface.co/api/datasets/{hf_id}"


class HttpClient(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> "HttpResponse": ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


def infer_hf_ref(seed: Seed) -> tuple[str, bool] | None:
    """Extract (hf_id, is_dataset) from the seed's hf_url, url or code_repository.

    hf_url (the entry's explicit HF page, when set) is checked first, then the
    primary url and repo. is_dataset is True when the matched URL is a
    /datasets/ page, so the right API endpoint is queried first.
    """
    for candidate in (seed.hf_url or "", seed.url, seed.code_repository or ""):
        m = _HF_ID_RE.search(candidate)
        if m:
            return m.group(1), bool(_HF_DATASET_RE.search(candidate))
    return None


def infer_hf_id(seed: Seed) -> str | None:
    """Back-compatible helper: the HF id alone, or None."""
    ref = infer_hf_ref(seed)
    return ref[0] if ref else None


class HuggingFaceClient:
    source_id = "huggingface"

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def fetch_signals(self, seed: Seed) -> list[HarvestedField]:
        now = datetime.now(timezone.utc)
        ref = infer_hf_ref(seed)
        if ref is None:
            return [self._status("hf_downloads", "not_found", "no HF id in seed", now, None)]
        hf_id, is_dataset = ref

        # Query the endpoint the URL implies first; a model id and a dataset id
        # share the 'org/name' shape, so on a 404 try the other kind before
        # giving up — this is what let a dataset (e.g. openai/healthbench) read
        # as not_found when only the models endpoint was tried.
        order = (_API_DATASET, _API_MODEL) if is_dataset else (_API_MODEL, _API_DATASET)

        last_status = "not_found"
        for api in order:
            try:
                resp = self._http.get(
                    api.format(hf_id=hf_id),
                    headers={"Accept": "application/json"},
                    timeout=15.0,
                )
            except Exception as exc:
                return [self._status("hf_downloads", "error", str(exc)[:120], now, hf_id)]

            if resp.status_code == 200:
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
            if resp.status_code == 429:
                return [self._status("hf_downloads", "rate_limited", "429", now, hf_id)]
            if resp.status_code == 404:
                last_status = "not_found"
                continue  # try the other endpoint kind
            last_status = str(resp.status_code)

        note = "no HF repo" if last_status == "not_found" else last_status
        return [self._status("hf_downloads", last_status, note, now, hf_id)]

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
