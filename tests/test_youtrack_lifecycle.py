from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from ironrag_connector.orchestrator import Orchestrator
from ironrag_connector.policy import PushPolicy
from ironrag_connector.routing import PolicyOverrides, Router, RoutingConfig
from ironrag_connector.state import StateStore
from ironrag_connector.sync import SyncManager

from youtrack_connector.adapter import YouTrackAdapter
from youtrack_connector.config import YouTrackSettings
from youtrack_connector.youtrack import YouTrackError, YouTrackNotFoundError

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000099")
LIBRARY_ID = UUID("00000000-0000-0000-0000-000000000077")


def _settings() -> YouTrackSettings:
    return YouTrackSettings(
        youtrack_base_url="https://tracker.example.com/youtrack",
        youtrack_token="perm:synthetic-token",
        ironrag_base_url="https://ironrag.example.com",
        ironrag_api_token="synthetic-ironrag-token",
        admin_bearer_token="synthetic-admin-token",
        sync_concurrency=1,
    )


def _article(
    article_id: str, readable_id: str, summary: str, content: str, updated: int
) -> dict[str, Any]:
    return {
        "id": article_id,
        "idReadable": readable_id,
        "summary": summary,
        "content": content,
        "created": updated - 1,
        "updated": updated,
        "project": {
            "id": "0-7",
            "shortName": "DOCS",
            "name": "Documentation",
            "archived": False,
        },
        "parentArticle": None,
        "reporter": {"id": "1-2", "login": "writer", "name": "Writer"},
        "tags": [],
        "attachments": [],
    }


class MutableYouTrack:
    def __init__(self, articles: list[dict[str, Any]]) -> None:
        self.articles = {article["id"]: article for article in articles}
        self.fetch_count = 0

    async def validate_credentials(self) -> dict[str, Any]:
        return {"id": "1-2", "login": "writer"}

    async def list_articles(self) -> AsyncIterator[dict[str, Any]]:
        for article_id in sorted(self.articles):
            yield deepcopy(self.articles[article_id])

    async def get_article(self, article_id: str) -> dict[str, Any]:
        self.fetch_count += 1
        if article_id not in self.articles:
            raise YouTrackNotFoundError(article_id)
        return deepcopy(self.articles[article_id])

    async def aclose(self) -> None:
        return None


class FailingEnumerationYouTrack(MutableYouTrack):
    async def list_articles(self) -> AsyncIterator[dict[str, Any]]:
        for index, article_id in enumerate(sorted(self.articles)):
            if index == 1:
                raise YouTrackError("synthetic second-page failure")
            yield deepcopy(self.articles[article_id])


class MemoryIronRag:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.deleted_ids: list[str] = []

    async def find_document_by_external_key(
        self, library_id: UUID, external_key: str
    ) -> dict[str, Any] | None:
        document = self.documents.get(external_key)
        if document and document["libraryId"] == str(library_id):
            return deepcopy(document)
        return None

    async def upload_document(self, **kwargs: Any) -> dict[str, Any]:
        external_key = str(kwargs["external_key"])
        document = {
            "id": str(uuid4()),
            "externalKey": external_key,
            "libraryId": str(kwargs["library_id"]),
            "title": kwargs.get("title"),
            "payload": bytes(kwargs["file_bytes"]),
            "documentHint": kwargs.get("document_hint"),
        }
        self.documents[external_key] = document
        return {"document": deepcopy(document)}

    async def replace_document(self, **kwargs: Any) -> dict[str, Any] | None:
        document_id = str(kwargs["document_id"])
        for document in self.documents.values():
            if document["id"] == document_id:
                document["payload"] = bytes(kwargs["file_bytes"])
                document["documentHint"] = kwargs.get("document_hint")
                return {"document": deepcopy(document)}
        return None

    async def list_documents_by_external_key_prefix(
        self, library_id: UUID, prefix: str, **_: Any
    ) -> list[tuple[str, str]]:
        return [
            (key, str(document["id"]))
            for key, document in self.documents.items()
            if key.startswith(prefix) and document["libraryId"] == str(library_id)
        ]

    async def delete_document(self, document_id: UUID | str, _: str) -> None:
        target = str(document_id)
        self.deleted_ids.append(target)
        for key, document in list(self.documents.items()):
            if document["id"] == target:
                del self.documents[key]

    async def get_document(self, document_id: UUID | str) -> dict[str, Any] | None:
        target = str(document_id)
        for document in self.documents.values():
            if document["id"] == target:
                return deepcopy(document)
        return None


def _manager(
    tmp_path: Path,
    vendor: MutableYouTrack,
    ironrag: MemoryIronRag,
) -> SyncManager:
    adapter = YouTrackAdapter(_settings(), client=vendor)
    router = Router(
        RoutingConfig.model_validate(
            {
                "default": {
                    "workspace": str(WORKSPACE_ID),
                    "library": str(LIBRARY_ID),
                }
            }
        )
    )
    state = StateStore(tmp_path / "state.sqlite")
    policies = PolicyOverrides(default=PushPolicy(), by_kind={})
    orchestrator = Orchestrator(
        adapter=adapter,
        ironrag=ironrag,  # type: ignore[arg-type]
        router=router,
        state=state,
        policies=policies,
    )
    return SyncManager(
        adapter=adapter,
        ironrag=ironrag,  # type: ignore[arg-type]
        orchestrator=orchestrator,
        router=router,
        state=state,
        policies=policies,
        concurrency=1,
        interval_seconds=60,
    )


@pytest.mark.asyncio
async def test_create_noop_restart_update_clear_and_delete_lifecycle(tmp_path: Path) -> None:
    first = _article("226-0", "DOCS-A-1", "First", "Initial body", 1000)
    second = _article("226-1", "DOCS-A-2", "Second", "Second body", 1000)
    vendor = MutableYouTrack([first, second])
    ironrag = MemoryIronRag()
    manager = _manager(tmp_path, vendor, ironrag)

    created = await manager.run_once(reason="initial")
    assert (created.created, created.errors) == (2, 0)
    first_document_id = ironrag.documents["youtrack:article:226-0"]["id"]
    initial_fetches = vendor.fetch_count

    unchanged = await manager.run_once(reason="unchanged")
    assert (unchanged.noop_unchanged, unchanged.created, unchanged.replaced) == (2, 0, 0)
    assert vendor.fetch_count == initial_fetches

    restarted = _manager(tmp_path, vendor, ironrag)
    after_restart = await restarted.run_once(reason="restart")
    assert after_restart.noop_unchanged == 2
    assert vendor.fetch_count == initial_fetches

    vendor.articles["226-0"].update(summary="First renamed", content="Updated body", updated=2000)
    updated = await restarted.run_once(reason="update")
    assert (updated.replaced, updated.noop_unchanged, updated.errors) == (1, 1, 0)
    document = ironrag.documents["youtrack:article:226-0"]
    assert document["id"] == first_document_id
    assert document["payload"].decode().startswith("# First renamed\n")
    assert "Updated body" in document["payload"].decode()

    vendor.articles["226-0"].update(content="", updated=3000)
    cleared = await restarted.run_once(reason="clear")
    assert cleared.replaced == 1
    assert ironrag.documents["youtrack:article:226-0"]["payload"] == b"# First renamed\n"

    deleted_source = vendor.articles.pop("226-1")
    deleted_document_id = ironrag.documents["youtrack:article:226-1"]["id"]
    deleted = await restarted.run_once(reason="delete")
    assert (deleted.reaped, deleted.errors) == (1, 0)
    assert "youtrack:article:226-1" not in ironrag.documents
    assert deleted_document_id in ironrag.deleted_ids
    assert deleted_source["id"] == "226-1"


@pytest.mark.asyncio
async def test_partial_enumeration_never_reaps_unseen_documents(tmp_path: Path) -> None:
    first = _article("226-0", "DOCS-A-1", "First", "Body", 1000)
    second = _article("226-1", "DOCS-A-2", "Second", "Body", 1000)
    healthy_vendor = MutableYouTrack([first, second])
    ironrag = MemoryIronRag()
    healthy = _manager(tmp_path, healthy_vendor, ironrag)
    assert (await healthy.run_once(reason="seed")).created == 2

    failing = _manager(tmp_path, FailingEnumerationYouTrack([first, second]), ironrag)
    report = await failing.run_once(reason="partial")

    assert report.errors == 1
    assert report.reaped == 0
    assert set(ironrag.documents) == {
        "youtrack:article:226-0",
        "youtrack:article:226-1",
    }
