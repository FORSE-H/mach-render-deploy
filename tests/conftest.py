"""Shared test fixtures — a fake HTTP client so adapter tests never touch the network.

The adapters take an injected HttpClient (httpx.Client satisfies it in production);
here a tiny fake returns canned responses keyed by a URL substring, so each test
asserts "given this response shape, the client produces these facts" without any
real request.
"""

from __future__ import annotations

import json as _json
from typing import Any


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, text: str | None = None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else (_json.dumps(body) if body is not None else "")

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeHttp:
    """Return a canned FakeResponse for the first route whose substring is in the URL.

    routes: list of (url_substring, FakeResponse). A request to a URL matching no
    route returns a 404, which is the honest 'not found' path the clients handle.
    Set `raise_on` to a substring to simulate a network exception for that URL.
    """

    def __init__(self, routes: list[tuple[str, FakeResponse]] | None = None, raise_on: str | None = None):
        self._routes = routes or []
        self._raise_on = raise_on
        self.calls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> FakeResponse:
        self.calls.append(url)
        if self._raise_on and self._raise_on in url:
            raise ConnectionError("simulated network failure")
        for sub, resp in self._routes:
            if sub in url:
                return resp
        return FakeResponse(404)
