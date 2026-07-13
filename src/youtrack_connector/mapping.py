from __future__ import annotations

CONNECTOR_NAME = "youtrack"
KIND_ARTICLE = "article"
KINDS: tuple[str, ...] = (KIND_ARTICLE,)

ARTICLE_PREFIX = f"{CONNECTOR_NAME}:{KIND_ARTICLE}:"


def article_external_key(article_id: str) -> str:
    return build_external_key(KIND_ARTICLE, article_id)


def build_external_key(kind: str, item_id: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unsupported YouTrack item kind: {kind}")
    return f"{CONNECTOR_NAME}:{kind}:{item_id}"


def parse_external_key(external_key: str) -> tuple[str, str] | None:
    prefix = f"{CONNECTOR_NAME}:"
    if not external_key.startswith(prefix):
        return None
    kind, separator, item_id = external_key[len(prefix) :].partition(":")
    if separator != ":" or kind not in KINDS or not item_id:
        return None
    return kind, item_id
