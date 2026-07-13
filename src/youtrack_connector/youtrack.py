from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
from ironrag_connector.observability import get_logger

from .config import YouTrackSettings

log = get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

ARTICLE_SUMMARY_FIELDS = ",".join(
    (
        "id",
        "idReadable",
        "summary",
        "created",
        "updated",
        "project(id,name,shortName,archived)",
        "reporter(id,login,name)",
        "parentArticle(id,idReadable,summary)",
        "tags(id,name)",
        "attachments(id,name,created,updated,size,mimeType,draft,removed)",
    )
)
ARTICLE_DETAIL_FIELDS = f"{ARTICLE_SUMMARY_FIELDS},content"


class YouTrackError(RuntimeError):
    """YouTrack request or response violated the connector contract."""


class YouTrackNotFoundError(YouTrackError):
    """An article disappeared between enumeration and materialization."""


class YouTrackClient:
    def __init__(
        self,
        settings: YouTrackSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._service_url = settings.youtrack_base_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.youtrack_token.get_secret_value()}",
        }
        if client is None:
            self._client = httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                headers=headers,
                follow_redirects=True,
            )
        else:
            client.headers.update(headers)
            self._client = client
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def validate_credentials(self) -> dict[str, Any]:
        response = await self._request(
            "GET",
            self._api_url("users/me"),
            params={"fields": "id,login,guest"},
        )
        self._raise_for_status(response, operation="validate credentials")
        user = _json_object(response, operation="validate credentials")
        if user.get("guest") is True or user.get("login") == "guest":
            raise YouTrackError("YouTrack token resolved to the guest account")
        if not isinstance(user.get("id"), str) or not user["id"]:
            raise YouTrackError("YouTrack credential response did not contain a user id")
        return user

    async def list_articles(self) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        seen_ids: set[str] = set()
        while True:
            response = await self._request(
                "GET",
                self._api_url("articles"),
                params={
                    "fields": ARTICLE_SUMMARY_FIELDS,
                    "$top": self._settings.youtrack_page_size,
                    "$skip": offset,
                },
            )
            self._raise_for_status(response, operation="list articles")
            page = _json_array(response, operation="list articles")
            if not page:
                return

            new_ids = 0
            for article in page:
                article_id = article.get("id")
                if not isinstance(article_id, str) or not article_id:
                    raise YouTrackError("YouTrack article collection contained an item without id")
                if article_id in seen_ids:
                    raise YouTrackError(
                        "YouTrack article pagination returned a duplicate article id; "
                        "retry next sweep"
                    )
                seen_ids.add(article_id)
                new_ids += 1
                yield article

            if new_ids == 0:
                raise YouTrackError("YouTrack article pagination made no progress")
            offset += len(page)

    async def get_article(self, article_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            self._api_url(f"articles/{quote(article_id, safe='')}"),
            params={"fields": ARTICLE_DETAIL_FIELDS},
        )
        if response.status_code == 404:
            raise YouTrackNotFoundError(f"article {article_id} not found")
        self._raise_for_status(response, operation="get article")
        return _json_object(response, operation="get article")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._settings.youtrack_retry_max + 1):
            await self._wait_for_rate_slot()
            try:
                response = await self._client.request(method, url, params=params)
            except httpx.RequestError as exc:
                if attempt >= self._settings.youtrack_retry_max:
                    raise YouTrackError(
                        f"YouTrack request failed after {attempt + 1} attempt(s)"
                    ) from exc
                delay = _backoff_delay(
                    attempt,
                    self._settings.youtrack_retry_backoff_seconds,
                    self._settings.youtrack_retry_max_sleep_seconds,
                )
                log.warning(
                    "youtrack.retry",
                    reason="transport_error",
                    error_type=type(exc).__name__,
                    attempt=attempt,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code not in RETRYABLE_STATUS:
                return response
            if attempt >= self._settings.youtrack_retry_max:
                return response
            delay = _retry_delay(
                response.headers,
                attempt,
                self._settings.youtrack_retry_backoff_seconds,
                self._settings.youtrack_retry_max_sleep_seconds,
            )
            log.warning(
                "youtrack.retry",
                status=response.status_code,
                attempt=attempt,
                delay_seconds=delay,
            )
            await asyncio.sleep(delay)

        raise AssertionError("retry loop must return or raise")  # pragma: no cover

    async def _wait_for_rate_slot(self) -> None:
        async with self._rate_lock:
            minimum = self._settings.youtrack_min_request_interval_seconds
            elapsed = time.monotonic() - self._last_request_at
            remaining = minimum - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_request_at = time.monotonic()

    def _api_url(self, resource: str) -> str:
        return f"{self._service_url}/api/{resource.lstrip('/')}"

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, operation: str) -> None:
        if response.status_code >= 400:
            raise YouTrackError(f"YouTrack {operation} failed with HTTP {response.status_code}")


def _json_object(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise YouTrackError(f"YouTrack {operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise YouTrackError(f"YouTrack {operation} did not return an object")
    return payload


def _json_array(response: httpx.Response, *, operation: str) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise YouTrackError(f"YouTrack {operation} returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise YouTrackError(f"YouTrack {operation} did not return a collection")
    if any(not isinstance(item, dict) for item in payload):
        raise YouTrackError(f"YouTrack {operation} returned a malformed collection")
    return payload


def _retry_delay(
    headers: httpx.Headers,
    attempt: int,
    backoff_seconds: float,
    max_sleep_seconds: float,
) -> float:
    retry_after = headers.get("Retry-After")
    if retry_after:
        stripped = retry_after.strip()
        if stripped.isdigit():
            return min(float(stripped), max_sleep_seconds)
        try:
            target = parsedate_to_datetime(stripped)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            delta = max(0.0, target.timestamp() - time.time())
            return min(delta, max_sleep_seconds)
        except (TypeError, ValueError, OverflowError):
            pass
    return _backoff_delay(attempt, backoff_seconds, max_sleep_seconds)


def _backoff_delay(attempt: int, backoff_seconds: float, max_sleep_seconds: float) -> float:
    return float(min(backoff_seconds * (2**attempt), max_sleep_seconds))
