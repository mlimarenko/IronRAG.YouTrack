from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from ironrag_connector.ironrag import (
    DocumentResource,
    IronRagNotFoundError,
    OperationHandle,
    OperationProgress,
    OperationStatus,
    OperationStatusValue,
    ProblemDetails,
)
from ironrag_connector.orchestrator import Orchestrator
from ironrag_connector.policy import PushPolicy
from ironrag_connector.routing import (
    PolicyOverrides,
    ResolvedLibraryTarget,
    Router,
    RoutingConfig,
)
from ironrag_connector.state import StateStore
from ironrag_connector.sync import SyncManager

from youtrack_connector.adapter import YouTrackAdapter
from youtrack_connector.config import YouTrackSettings
from youtrack_connector.youtrack import YouTrackError, YouTrackNotFoundError

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000099")
LIBRARY_ID = UUID("00000000-0000-0000-0000-000000000077")
LIBRARY_REF = "default/youtrack-knowledge-base"


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


def _not_found_error(document_id: str) -> IronRagNotFoundError:
    return IronRagNotFoundError(
        ProblemDetails(
            type="urn:ironrag:error:not_found",
            title="Not Found",
            status=404,
            detail=f"synthetic not found: {document_id}",
            code="not_found",
        )
    )


def _ready_operation(operation_id: UUID) -> OperationStatus:
    return OperationStatus(
        id=operation_id,
        workspace_id=WORKSPACE_ID,
        library_id=LIBRARY_ID,
        operation_kind="synthetic",
        status=OperationStatusValue.READY,
        created_at=datetime.now(tz=UTC),
        progress=OperationProgress(),
    )


class MemoryIronRag:
    """In-memory double for the redesigned :class:`IronRagClient` surface:
    synchronous ``create_document``, asynchronous ``create_revision``/
    ``delete_document`` polled via ``wait_for_operation``, and
    ``find_document``/``list_documents`` reads."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.deleted_ids: list[str] = []
        self._operations: dict[UUID, OperationStatus] = {}

    def _resource(self, document: dict[str, Any]) -> DocumentResource:
        return DocumentResource(
            id=UUID(document["id"]),
            library_id=UUID(document["libraryId"]),
            external_key=document["externalKey"],
            status="ready",
            document_hint=document["documentHint"],
        )

    def _admit(self) -> OperationHandle:
        operation_id = uuid4()
        self._operations[operation_id] = _ready_operation(operation_id)
        return OperationHandle(operation_id=operation_id)

    async def find_document(self, library_id: UUID, external_key: str) -> DocumentResource | None:
        document = self.documents.get(external_key)
        if document and document["libraryId"] == str(library_id):
            return self._resource(document)
        return None

    async def list_documents(
        self,
        library_id: UUID,
        *,
        search: str | None = None,
        external_key: str | None = None,
        status: Sequence[str] = (),
        include_deleted: bool = False,
        limit: int = 200,
    ) -> AsyncIterator[DocumentResource]:
        for key in sorted(self.documents):
            document = self.documents[key]
            if document["libraryId"] != str(library_id):
                continue
            if external_key and key != external_key:
                continue
            if search and search not in key:
                continue
            yield self._resource(document)

    async def create_document(
        self,
        library_id: UUID,
        *,
        external_key: str,
        file_bytes: bytes | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
        title: str | None = None,
        document_hint: str | None = None,
        parent_external_key: str | None = None,
    ) -> DocumentResource:
        document = {
            "id": str(uuid4()),
            "externalKey": external_key,
            "libraryId": str(library_id),
            "title": title,
            "payload": bytes(file_bytes or b""),
            "documentHint": document_hint,
        }
        self.documents[external_key] = document
        return self._resource(document)

    async def create_revision(
        self,
        document_id: UUID | str,
        *,
        mode: str,
        markdown: str | None = None,
        appended_text: str | None = None,
        file_bytes: bytes | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
        idempotency_key: str,
    ) -> OperationHandle:
        target = str(document_id)
        for document in self.documents.values():
            if document["id"] == target:
                document["payload"] = bytes(file_bytes or b"")
                return self._admit()
        raise _not_found_error(target)

    async def delete_document(
        self, document_id: UUID | str, *, idempotency_key: str
    ) -> OperationHandle | None:
        target = str(document_id)
        for key, document in list(self.documents.items()):
            if document["id"] == target:
                del self.documents[key]
                self.deleted_ids.append(target)
                return self._admit()
        return None

    async def wait_for_operation(
        self,
        operation_id: UUID | str,
        *,
        poll_interval: float | None = None,
        budget: float | None = None,
    ) -> OperationStatus:
        return self._operations[UUID(str(operation_id))]


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
                    "library": LIBRARY_REF,
                }
            }
        ),
        resolved_targets={
            LIBRARY_REF: ResolvedLibraryTarget(
                library_ref=LIBRARY_REF,
                workspace_id=WORKSPACE_ID,
                library_id=LIBRARY_ID,
            )
        },
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
