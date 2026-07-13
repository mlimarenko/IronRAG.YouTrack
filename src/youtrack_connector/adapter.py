from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol
from urllib.parse import quote

from ironrag_connector import SourceAdapter, SourceItem, SourceItemRef

from .config import YouTrackSettings
from .mapping import (
    CONNECTOR_NAME,
    KIND_ARTICLE,
    KINDS,
    article_external_key,
    build_external_key,
    parse_external_key,
)
from .youtrack import YouTrackClient, YouTrackError, YouTrackNotFoundError

_FILENAME_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


class YouTrackApi(Protocol):
    async def validate_credentials(self) -> dict[str, Any]: ...

    def list_articles(self) -> AsyncIterator[dict[str, Any]]: ...

    async def get_article(self, article_id: str) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class YouTrackAdapter(SourceAdapter):
    name = CONNECTOR_NAME
    kinds = KINDS
    primary_kinds = (KIND_ARTICLE,)

    def __init__(
        self,
        settings: YouTrackSettings,
        client: YouTrackApi | None = None,
    ) -> None:
        self._settings = settings
        self._client: YouTrackApi = client or YouTrackClient(settings)
        self._base_url = settings.youtrack_base_url.rstrip("/")
        self._included_projects = set(settings.included_projects())
        self._excluded_projects = set(settings.excluded_projects())

    async def close(self) -> None:
        await self._client.aclose()

    def external_key(self, kind: str, item_id: str) -> str:
        return build_external_key(kind, item_id)

    def parse_external_key(self, external_key: str) -> tuple[str, str] | None:
        return parse_external_key(external_key)

    async def iter_items(self) -> AsyncIterator[SourceItemRef]:
        initial_identity = await self._client.validate_credentials()
        initial_identity_id = _required_string(initial_identity, "id")
        previous_tokens: dict[str, str] | None = None
        snapshot: dict[str, dict[str, Any]] = {}
        for _ in range(self._settings.youtrack_snapshot_validation_passes):
            current: dict[str, dict[str, Any]] = {}
            async for article in self._client.list_articles():
                article_id = _required_string(article, "id")
                if not self._include_article(article):
                    continue
                current[article_id] = article
            current_tokens = {
                article_id: _article_change_token(article)
                for article_id, article in current.items()
            }
            if previous_tokens is not None and current_tokens != previous_tokens:
                raise YouTrackError(
                    "YouTrack snapshot changed between validation passes; retry next sweep"
                )
            previous_tokens = current_tokens
            snapshot = current

        final_identity = await self._client.validate_credentials()
        if _required_string(final_identity, "id") != initial_identity_id:
            raise YouTrackError(
                "YouTrack authenticated identity changed during snapshot validation; "
                "retry next sweep"
            )

        for article_id, article in snapshot.items():
            yield self._ref(article_id, article)

    async def fetch(self, ref: SourceItemRef) -> SourceItem | None:
        if ref.kind != KIND_ARTICLE:
            return None
        try:
            article = await self._client.get_article(ref.item_id)
        except YouTrackNotFoundError:
            return None

        article_id = _required_string(article, "id")
        if article_id != ref.item_id:
            raise YouTrackError(
                f"YouTrack returned article {article_id} while fetching {ref.item_id}"
            )
        if not self._include_article(article):
            return None
        current_ref = self._ref(article_id, article)
        if (
            current_ref.change_token != ref.change_token
            or current_ref.routing_facts != ref.routing_facts
        ):
            return None
        title = _article_title(article)
        readable_id = _optional_string(article.get("idReadable")) or article_id
        payload = _render_markdown(title, article)
        return SourceItem(
            ref=current_ref,
            payload=payload.encode("utf-8"),
            mime_type="text/markdown",
            file_name=f"{_safe_filename(readable_id)}.md",
            title=title,
            document_hint=(f"{self._base_url}/articles/{quote(readable_id, safe='')}"),
        )

    def _ref(self, article_id: str, article: dict[str, Any]) -> SourceItemRef:
        return SourceItemRef(
            item_id=article_id,
            kind=KIND_ARTICLE,
            external_key=article_external_key(article_id),
            change_token=_article_change_token(article),
            routing_facts=_routing_facts(article),
            raw=article,
        )

    def _include_article(self, article: dict[str, Any]) -> bool:
        project = article.get("project")
        if not isinstance(project, dict):
            raise YouTrackError("YouTrack article did not contain project metadata")
        selectors = {
            value
            for value in (
                _optional_string(project.get("id")),
                _optional_string(project.get("shortName")),
            )
            if value is not None
        }
        if not selectors:
            raise YouTrackError("YouTrack article project did not contain an id")
        if self._included_projects and not (selectors & self._included_projects):
            return False
        if selectors & self._excluded_projects:
            return False
        return not (
            project.get("archived") is True
            and not self._settings.youtrack_include_archived_projects
        )


def _article_change_token(article: dict[str, Any]) -> str:
    project = _dict_or_empty(article.get("project"))
    parent = _dict_or_empty(article.get("parentArticle"))
    reporter = _dict_or_empty(article.get("reporter"))
    snapshot = {
        "updated": article.get("updated"),
        "summary": article.get("summary"),
        "project": {
            "id": project.get("id"),
            "shortName": project.get("shortName"),
            "name": project.get("name"),
        },
        "parent": {
            "id": parent.get("id"),
            "idReadable": parent.get("idReadable"),
        },
        "reporter": {"id": reporter.get("id"), "login": reporter.get("login")},
        "tags": _tag_descriptors(article.get("tags")),
        "attachments": _attachment_descriptors(article.get("attachments")),
    }
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _routing_facts(article: dict[str, Any]) -> dict[str, Any]:
    project = _dict_or_empty(article.get("project"))
    parent = _dict_or_empty(article.get("parentArticle"))
    reporter = _dict_or_empty(article.get("reporter"))
    tags = sorted(
        {
            name
            for tag in _dict_items(article.get("tags"))
            if (name := _optional_string(tag.get("name"))) is not None
        }
    )
    return {
        "article_id": _optional_string(article.get("id")),
        "article_id_readable": _optional_string(article.get("idReadable")),
        "project_id": _optional_string(project.get("id")),
        "project": _optional_string(project.get("shortName")),
        "project_short_name": _optional_string(project.get("shortName")),
        "project_name": _optional_string(project.get("name")),
        "parent_article_id": _optional_string(parent.get("id")),
        "parent_article_id_readable": _optional_string(parent.get("idReadable")),
        "tag": tags,
        "reporter_login": _optional_string(reporter.get("login")),
    }


def _render_markdown(title: str, article: dict[str, Any]) -> str:
    heading = " ".join(title.split()) or "Untitled"
    content = article.get("content")
    if content is not None and not isinstance(content, str):
        raise YouTrackError("YouTrack article content was not a string or null")
    body = content.strip("\r\n") if isinstance(content, str) else ""
    if not body.strip():
        body = ""
    sections: list[str] = [f"# {heading}"]
    if body:
        sections.append(body)

    attachment_lines: list[str] = []
    for attachment in _dict_items(article.get("attachments")):
        if attachment.get("removed") is True or attachment.get("draft") is True:
            continue
        name = _optional_string(attachment.get("name"))
        if not name:
            continue
        fields = [f"**{_escape_markdown(name)}**"]
        mime_type = _optional_string(attachment.get("mimeType"))
        if mime_type:
            fields.append(f"`{mime_type.replace('`', '')}`")
        size = attachment.get("size")
        if isinstance(size, int) and size >= 0:
            fields.append(f"{size} B")
        attachment_lines.append("- " + " — ".join(fields))
    if attachment_lines:
        sections.append("\n".join(attachment_lines))
    return "\n\n".join(sections) + "\n"


def _attachment_descriptors(raw: Any) -> list[dict[str, Any]]:
    descriptors = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "created": item.get("created"),
            "updated": item.get("updated"),
            "size": item.get("size"),
            "mimeType": item.get("mimeType"),
            "draft": item.get("draft"),
            "removed": item.get("removed"),
        }
        for item in _dict_items(raw)
    ]
    return sorted(descriptors, key=lambda item: str(item.get("id") or ""))


def _tag_descriptors(raw: Any) -> list[dict[str, Any]]:
    descriptors = [{"id": item.get("id"), "name": item.get("name")} for item in _dict_items(raw)]
    return sorted(descriptors, key=lambda item: (str(item.get("id")), str(item.get("name"))))


def _dict_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _dict_or_empty(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _article_title(article: dict[str, Any]) -> str:
    return (
        _optional_string(article.get("summary"))
        or _optional_string(article.get("idReadable"))
        or _required_string(article, "id")
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = _optional_string(payload.get(key))
    if value is None:
        raise YouTrackError(f"YouTrack payload did not contain {key}")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_filename(value: str) -> str:
    collapsed = _FILENAME_UNSAFE_RE.sub("-", value.strip()).strip("-.")
    return collapsed or "article"


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")
