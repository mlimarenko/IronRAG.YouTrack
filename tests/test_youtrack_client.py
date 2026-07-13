from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from youtrack_connector.config import YouTrackSettings
from youtrack_connector.youtrack import (
    YouTrackClient,
    YouTrackError,
    YouTrackNotFoundError,
)


def _settings(**overrides: object) -> YouTrackSettings:
    data: dict[str, object] = {
        "youtrack_base_url": "https://tracker.example.com/youtrack",
        "youtrack_token": "perm:synthetic-token",
        "youtrack_page_size": 2,
        "youtrack_min_request_interval_seconds": 0,
        "ironrag_base_url": "https://ironrag.example.com",
        "ironrag_api_token": "synthetic-ironrag-token",
        "admin_bearer_token": "synthetic-admin-token",
    }
    data.update(overrides)
    return YouTrackSettings(**data)


@pytest.mark.asyncio
async def test_list_articles_paginates_and_preserves_context_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        skip = int(request.url.params.get("$skip", "0"))
        if skip == 0:
            return httpx.Response(200, json=[{"id": "226-0"}, {"id": "226-1"}])
        if skip == 2:
            return httpx.Response(200, json=[{"id": "226-2"}])
        if skip == 3:
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected pagination offset {skip}")

    http = httpx.AsyncClient(
        base_url="https://tracker.example.com/youtrack/",
        transport=httpx.MockTransport(handler),
    )
    api = YouTrackClient(_settings(), client=http)

    articles = [article async for article in api.list_articles()]

    assert [article["id"] for article in articles] == ["226-0", "226-1", "226-2"]
    assert [request.url.path for request in requests] == [
        "/youtrack/api/articles",
        "/youtrack/api/articles",
        "/youtrack/api/articles",
    ]
    assert requests[0].url.params["$top"] == "2"
    assert requests[0].url.params["$skip"] == "0"
    assert "content" not in requests[0].url.params["fields"]
    assert "attachments(" in requests[0].url.params["fields"]
    assert requests[0].headers["Authorization"] == "Bearer perm:synthetic-token"

    await api.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_get_article_requests_full_content() -> None:
    captured_fields = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_fields
        captured_fields = request.url.params.get("fields", "")
        return httpx.Response(
            200,
            json={
                "id": "226-0",
                "idReadable": "DOCS-A-1",
                "summary": "Overview",
                "content": "Body",
            },
        )

    http = httpx.AsyncClient(
        base_url="https://tracker.example.com/youtrack/",
        transport=httpx.MockTransport(handler),
    )
    api = YouTrackClient(_settings(), client=http)

    article = await api.get_article("226-0")

    assert article["content"] == "Body"
    assert "content" in captured_fields.split(",")
    await api.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_retry_after_is_honoured_for_rate_limit() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=[])

    http = httpx.AsyncClient(
        base_url="https://tracker.example.com/youtrack/",
        transport=httpx.MockTransport(handler),
    )
    api = YouTrackClient(
        _settings(youtrack_retry_max=1, youtrack_retry_max_sleep_seconds=10),
        client=http,
    )

    with patch("youtrack_connector.youtrack.asyncio.sleep", new=AsyncMock()) as sleep:
        assert [article async for article in api.list_articles()] == []

    sleep.assert_awaited_once_with(3.0)
    assert attempts == 2
    await api.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_get_article_maps_404_to_soft_not_found() -> None:
    http = httpx.AsyncClient(
        base_url="https://tracker.example.com/youtrack/",
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
    )
    api = YouTrackClient(_settings(), client=http)

    with pytest.raises(YouTrackNotFoundError):
        await api.get_article("226-404")

    await api.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_api_errors_do_not_expose_token() -> None:
    http = httpx.AsyncClient(
        base_url="https://tracker.example.com/youtrack/",
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text="denied")),
    )
    api = YouTrackClient(_settings(youtrack_token="perm:must-not-leak"), client=http)

    with pytest.raises(YouTrackError) as raised:
        _ = [article async for article in api.list_articles()]

    assert "must-not-leak" not in str(raised.value)
    await api.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_invalid_collection_payload_fails_closed() -> None:
    http = httpx.AsyncClient(
        base_url="https://tracker.example.com/youtrack/",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "bad"})),
    )
    api = YouTrackClient(_settings(), client=http)

    with pytest.raises(YouTrackError, match="collection"):
        _ = [article async for article in api.list_articles()]

    await api.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_duplicate_id_during_offset_pagination_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("$skip", "0"))
        if skip == 0:
            return httpx.Response(200, json=[{"id": "226-0"}, {"id": "226-1"}])
        if skip == 2:
            return httpx.Response(200, json=[{"id": "226-1"}, {"id": "226-2"}])
        raise AssertionError(f"unexpected pagination offset {skip}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = YouTrackClient(_settings(), client=http)

    with pytest.raises(YouTrackError, match="duplicate article id"):
        _ = [article async for article in api.list_articles()]

    await api.aclose()
    await http.aclose()


@pytest.mark.asyncio
async def test_guest_identity_is_rejected() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"id": "1-0", "login": "guest", "guest": True})
        ),
    )
    api = YouTrackClient(_settings(), client=http)

    with pytest.raises(YouTrackError, match="guest"):
        await api.validate_credentials()

    await api.aclose()
    await http.aclose()
