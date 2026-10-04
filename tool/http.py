from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tool.errors import TransportError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    data: Any
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        if query:
            encoded = urlencode(
                {key: value for key, value in query.items() if value is not None},
                doseq=True,
            )
            url = f"{url}{'&' if '?' in url else '?'}{encoded}"
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        request = Request(url, data=body, headers=request_headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return HttpResponse(
                    response.status, self._decode(response.read()), dict(response.headers.items())
                )
        except HTTPError as exc:
            return HttpResponse(
                exc.code,
                self._decode(exc.read()),
                dict(exc.headers.items()) if exc.headers else {},
            )
        except (URLError, socket.timeout, TimeoutError) as exc:
            raise TransportError(f"HTTP request failed: {exc}") from exc

    @staticmethod
    def _decode(payload: bytes) -> Any:
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError("Remote endpoint returned invalid JSON") from exc
