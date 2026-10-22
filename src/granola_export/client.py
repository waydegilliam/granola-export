from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from granola_export.errors import ApiError, AuthError, ShapeError

API_BASE = "https://api.granola.ai"
ALLOWED_HOSTS = {
    "api.granola.ai",
    "berry.api.granola.ai",
    "chia.api.granola.ai",
    "maple.api.granola.ai",
    "pecan.api.granola.ai",
}


def records(data: Any, *keys: str) -> list[dict]:
    if isinstance(data, dict):
        for key in keys:
            if key in data:
                data = data[key]
                break
        else:
            raise ShapeError(f"Expected a collection under one of: {', '.join(keys)}.")
    if isinstance(data, dict):
        data = list(data.values())
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ShapeError("Expected an array or ID-keyed map of records.")
    return data


class GranolaClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30,
        retries: int = 4,
        delay: float = 0.2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self.token = token
        self.retries = retries
        self.delay = delay
        self.sleep = sleep

    def __enter__(self) -> GranolaClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.http.close()

    def post(self, endpoint: str, body: dict | None = None, *, base: str = API_BASE) -> Any:
        url = httpx.URL(base + endpoint)
        if url.scheme != "https" or url.host not in ALLOWED_HOSTS:
            raise ValueError("Credentials may only be sent to known Granola API hosts.")
        for attempt in range(self.retries + 1):
            self.sleep(self.delay)
            try:
                response = self.http.post(
                    url,
                    json=body or {},
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/json",
                        "User-Agent": "granola-export/0.1.0",
                    },
                )
            except httpx.TransportError:
                if attempt == self.retries:
                    raise ApiError(endpoint, None, "network request failed") from None
                self.sleep(min(2**attempt, 30))
                continue
            if response.status_code == 401:
                raise AuthError(
                    "Granola rejected the session. Authenticate again and rerun the export."
                )
            if response.status_code in {408, 429, 500, 502, 503, 504} and attempt < self.retries:
                self.sleep(self.retry_delay(response, attempt))
                continue
            if not response.is_success:
                raise ApiError(endpoint, response.status_code, f"HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError:
                raise ShapeError(f"{endpoint}: expected JSON, received another format.") from None
            if isinstance(data, dict) and data.get("error"):
                raise ApiError(endpoint, response.status_code, "server returned an error payload")
            return data
        raise AssertionError("Unreachable retry state")

    @staticmethod
    def retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return max(0, float(value))
            except ValueError:
                try:
                    return max(
                        0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
        return min(2**attempt, 30)

    def pages(
        self,
        endpoint: str,
        *keys: str,
        body: dict | None = None,
        page_size: int = 100,
    ) -> Iterator[Any]:
        offset = 0
        cursor = None
        seen = set()
        seen_cursors = set()
        for _ in range(100000):
            params = {**(body or {}), "limit": page_size, "offset": offset}
            if cursor is not None:
                params["cursor"] = cursor
            data = self.post(endpoint, params)
            items = records(data, *keys)
            ids = {str(item.get("id", item.get("document_id", ""))) for item in items}
            if items and ids <= seen:
                raise ShapeError(
                    f"{endpoint}: pagination repeated records; export may be incomplete."
                )
            seen.update(ids)
            yield data
            more = data.get("has_more") if isinstance(data, dict) else None
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            next_offset = data.get("next_offset") if isinstance(data, dict) else None
            if more is False:
                return
            if next_cursor is not None:
                key = str(next_cursor)
                if key in seen_cursors:
                    raise ShapeError(f"{endpoint}: cursor did not advance.")
                seen_cursors.add(key)
                cursor = next_cursor
            elif isinstance(next_offset, int):
                if next_offset <= offset:
                    raise ShapeError(f"{endpoint}: offset did not advance.")
                offset = next_offset
            elif more is True:
                if not items:
                    raise ShapeError(f"{endpoint}: empty page claims there are more records.")
                offset += len(items)
            elif len(items) < page_size:
                return
            else:
                offset += len(items)
        raise ShapeError(f"{endpoint}: pagination safety limit reached.")
