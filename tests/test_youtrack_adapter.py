from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy

import pytest

from youtrack_connector.adapter import YouTrackAdapter
from youtrack_connector.config import YouTrackSettings
from youtrack_connector.mapping import KIND_ARTICLE
from youtrack_connector.youtrack import YouTrackError, YouTrackNotFoundError


def _settings(**overrides: object) -> YouTrackSettings:
    data: dict[str, object] = {
        "youtrack_base_url": "https://tracker.example.com/youtrack",
        "youtrack_token": "perm:synthetic-token",
        "ironrag_base_url": "https://ironrag.example.com",
        "ironrag_api_token": "synthetic-ironrag-token",
        "admin_bearer_token": "synthetic-admin-token",
    }
    data.update(overrides)
    return YouTrackSettings(**data)


def _article(**overrides: object) -> dict[str, object]:
    article: dict[str, object] = {
        "id": "226-0",
        "idReadable": "DOCS-A-1",
        "summary": "Neutral Overview",
        "content": "A reusable body with `code` and a table.\n\n| A | B |\n|---|---|\n| 1 | 2 |",
        "created": 1_700_000_000_000,
        "updated": 1_700_000_001_000,
        "project": {
            "id": "0-7",
            "shortName": "DOCS",
            "name": "Documentation",
            "archived": False,
        },
        "parentArticle": {
            "id": "226-parent",
            "idReadable": "DOCS-A-0",
            "summary": "Root",
        },
        "reporter": {"id": "1-2", "login": "writer", "name": "Writer"},
        "tags": [
            {"id": "6-2", "name": "beta"},
            {"id": "6-1", "name": "alpha"},
        ],
        "attachments": [
            {
                "id": "237-3",
                "name": "diagram.png",
                "updated": 1_700_000_000_500,
                "size": 1234,
                "mimeType": "image/png",
                "draft": False,
                "removed": False,
                "url": "/youtrack/api/files/237-3?sign=must-not-be-indexed",
            }
        ],
    }
    article.update(overrides)
    return article


class FakeYouTrackClient:
    def __init__(
        self, summaries: list[dict[str, object]], details: dict[str, dict[str, object]]
    ) -> None:
        self.summaries = summaries
        self.details = details
        self.fetches: list[str] = []
        self.validation_count = 0
        self.closed = False

    async def validate_credentials(self) -> dict[str, object]:
        self.validation_count += 1
        return {"id": "1-2", "login": "writer"}

    async def list_articles(self) -> AsyncIterator[dict[str, object]]:
        for article in self.summaries:
            yield deepcopy(article)

    async def get_article(self, article_id: str) -> dict[str, object]:
        self.fetches.append(article_id)
        try:
            return deepcopy(self.details[article_id])
        except KeyError as exc:
            raise YouTrackNotFoundError(article_id) from exc

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_iter_items_emits_stable_identity_routing_and_composite_token() -> None:
    source = _article()
    client = FakeYouTrackClient([source], {"226-0": source})
    adapter = YouTrackAdapter(_settings(), client=client)

    refs = [ref async for ref in adapter.iter_items()]

    assert len(refs) == 1
    ref = refs[0]
    assert ref.kind == KIND_ARTICLE
    assert ref.item_id == "226-0"
    assert ref.external_key == "youtrack:article:226-0"
    assert ref.change_token is not None and len(ref.change_token) == 64
    assert ref.routing_facts == {
        "article_id": "226-0",
        "article_id_readable": "DOCS-A-1",
        "project_id": "0-7",
        "project": "DOCS",
        "project_short_name": "DOCS",
        "project_name": "Documentation",
        "parent_article_id": "226-parent",
        "parent_article_id_readable": "DOCS-A-0",
        "tag": ["alpha", "beta"],
        "reporter_login": "writer",
    }
    assert client.validation_count == 2


@pytest.mark.asyncio
async def test_change_token_detects_attachment_only_and_summary_changes() -> None:
    original = _article()
    attachment_changed = deepcopy(original)
    attachments = attachment_changed["attachments"]
    assert isinstance(attachments, list)
    assert isinstance(attachments[0], dict)
    attachments[0]["updated"] = 1_700_000_009_999
    summary_changed = deepcopy(original)
    summary_changed["summary"] = "Renamed Overview"

    original_ref = await _single_ref(original)
    attachment_ref = await _single_ref(attachment_changed)
    summary_ref = await _single_ref(summary_changed)

    assert original_ref.change_token != attachment_ref.change_token
    assert original_ref.change_token != summary_ref.change_token
    assert original_ref.external_key == summary_ref.external_key


@pytest.mark.asyncio
async def test_attachment_order_does_not_change_token() -> None:
    first = _article(
        attachments=[
            {"id": "2", "name": "b.txt", "updated": 2, "size": 2},
            {"id": "1", "name": "a.txt", "updated": 1, "size": 1},
        ]
    )
    second = deepcopy(first)
    attachments = second["attachments"]
    assert isinstance(attachments, list)
    attachments.reverse()

    assert (await _single_ref(first)).change_token == (await _single_ref(second)).change_token


@pytest.mark.asyncio
async def test_fetch_renders_title_body_and_safe_attachment_index() -> None:
    summary = _article(summary="Current title", content=None)
    detail = _article(summary="Current title")
    client = FakeYouTrackClient([summary], {"226-0": detail})
    adapter = YouTrackAdapter(_settings(), client=client)
    ref = await _first(adapter.iter_items())

    item = await adapter.fetch(ref)

    assert item is not None
    assert item.ref.external_key == ref.external_key
    assert item.title == "Current title"
    assert item.mime_type == "text/markdown"
    assert item.file_name == "DOCS-A-1.md"
    assert item.document_hint == "https://tracker.example.com/youtrack/articles/DOCS-A-1"
    markdown = item.payload.decode("utf-8")
    assert markdown.startswith("# Current title\n")
    assert "A reusable body" in markdown
    assert "diagram.png" in markdown
    assert "image/png" in markdown
    assert "must-not-be-indexed" not in markdown
    assert item.dependents == ()


@pytest.mark.asyncio
async def test_fetch_defers_article_that_changed_after_stable_snapshot() -> None:
    summary = _article(summary="Old title", content=None)
    detail = _article(summary="Current title")
    client = FakeYouTrackClient([summary], {"226-0": detail})
    adapter = YouTrackAdapter(_settings(), client=client)
    ref = await _first(adapter.iter_items())

    assert await adapter.fetch(ref) is None


@pytest.mark.asyncio
async def test_fetch_defers_article_that_moved_outside_project_filter() -> None:
    summary = _article()
    detail = _article(
        project={
            "id": "0-8",
            "shortName": "PRIVATE",
            "name": "Private",
            "archived": False,
        }
    )
    client = FakeYouTrackClient([summary], {"226-0": detail})
    adapter = YouTrackAdapter(_settings(youtrack_projects="DOCS"), client=client)
    ref = await _first(adapter.iter_items())

    assert await adapter.fetch(ref) is None


@pytest.mark.asyncio
async def test_fetch_preserves_indented_markdown_code_block() -> None:
    article = _article(content="\n    indented code\n", attachments=[])
    client = FakeYouTrackClient([article], {"226-0": article})
    adapter = YouTrackAdapter(_settings(), client=client)
    ref = await _first(adapter.iter_items())

    item = await adapter.fetch(ref)

    assert item is not None
    assert item.payload.decode("utf-8") == "# Neutral Overview\n\n    indented code\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", "  \n"])
async def test_fetch_never_leaves_stale_content_for_empty_article(content: object) -> None:
    article = _article(content=content, attachments=[])
    client = FakeYouTrackClient([article], {"226-0": article})
    adapter = YouTrackAdapter(_settings(), client=client)
    ref = await _first(adapter.iter_items())

    item = await adapter.fetch(ref)

    assert item is not None
    assert item.payload.decode("utf-8") == "# Neutral Overview\n"


@pytest.mark.asyncio
async def test_project_filters_and_archived_projects_are_applied_before_sync() -> None:
    included = _article()
    excluded = _article(
        id="226-1",
        idReadable="ARCHIVE-A-1",
        project={"id": "0-8", "shortName": "ARCHIVE", "name": "Archive", "archived": False},
    )
    archived = _article(
        id="226-2",
        idReadable="DOCS-A-2",
        project={"id": "0-7", "shortName": "DOCS", "name": "Documentation", "archived": True},
    )
    client = FakeYouTrackClient(
        [included, excluded, archived],
        {"226-0": included, "226-1": excluded, "226-2": archived},
    )
    adapter = YouTrackAdapter(
        _settings(youtrack_projects="DOCS", youtrack_exclude_projects="ARCHIVE"),
        client=client,
    )

    refs = [ref async for ref in adapter.iter_items()]

    assert [ref.item_id for ref in refs] == ["226-0"]


@pytest.mark.asyncio
async def test_fetch_returns_none_when_article_was_deleted_after_listing() -> None:
    article = _article()
    client = FakeYouTrackClient([article], {})
    adapter = YouTrackAdapter(_settings(), client=client)
    ref = await _first(adapter.iter_items())

    assert await adapter.fetch(ref) is None


@pytest.mark.asyncio
async def test_close_releases_vendor_client() -> None:
    client = FakeYouTrackClient([], {})
    adapter = YouTrackAdapter(_settings(), client=client)

    await adapter.close()

    assert client.closed is True


class ChangingSnapshotClient(FakeYouTrackClient):
    def __init__(self) -> None:
        first = _article()
        second = _article(summary="Changed during pagination")
        super().__init__([first], {"226-0": second})
        self._passes = [[first], [second]]

    async def list_articles(self) -> AsyncIterator[dict[str, object]]:
        current = self._passes.pop(0)
        for article in current:
            yield deepcopy(article)


class ChangingIdentityClient(FakeYouTrackClient):
    def __init__(self) -> None:
        article = _article()
        super().__init__([article], {"226-0": article})
        self._identities = [
            {"id": "1-2", "login": "writer"},
            {"id": "1-3", "login": "other"},
        ]

    async def validate_credentials(self) -> dict[str, object]:
        self.validation_count += 1
        return self._identities.pop(0)


@pytest.mark.asyncio
async def test_inconsistent_validation_snapshots_abort_before_yielding() -> None:
    adapter = YouTrackAdapter(_settings(), client=ChangingSnapshotClient())

    with pytest.raises(YouTrackError, match="changed between validation passes"):
        _ = [ref async for ref in adapter.iter_items()]


@pytest.mark.asyncio
async def test_authenticated_identity_change_after_snapshots_aborts_before_yielding() -> None:
    adapter = YouTrackAdapter(_settings(), client=ChangingIdentityClient())

    with pytest.raises(YouTrackError, match="authenticated identity changed"):
        _ = [ref async for ref in adapter.iter_items()]


async def _single_ref(article: dict[str, object]):
    client = FakeYouTrackClient([article], {str(article["id"]): article})
    adapter = YouTrackAdapter(_settings(), client=client)
    return await _first(adapter.iter_items())


async def _first(iterator: AsyncIterator[object]):
    async for item in iterator:
        return item
    raise AssertionError("expected one item")
