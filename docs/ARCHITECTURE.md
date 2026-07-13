# Architecture

## Data flow

```text
YouTrack /api/articles
        │  two complete, identical paginated passes
        ▼
YouTrackAdapter.iter_items
        │  SourceItemRef(id, routing facts, composite change token)
        ▼
ConnectorTemplate cursor + router + orchestrator
        │  upload / noop / replace / reap
        ▼
IronRAG /v1/content/documents/*
```

The adapter validates the permanent-token identity before every sweep. It then
collects two snapshots and validates the same identity again. IDs and composite
tokens must match exactly; duplicate IDs are treated as an offset shift.
Otherwise enumeration raises and the framework does not run destructive
reconciliation. This protects `$skip`/`$top` offset pagination from concurrent
inserts, deletes, edits, or credential revocation during a scan.

## Identity and diff

- Source identity: YouTrack database `Article.id`.
- IronRAG identity: `youtrack:article:<Article.id>`.
- Citation URL: `<YOUTRACK_BASE_URL>/articles/<Article.idReadable>`.
- MIME: `text/markdown`.

The change token hashes a canonical JSON snapshot of `updated`, `summary`,
project identity, parent identity, reporter identity, sorted tags, and sorted
attachment descriptors. Signed attachment URLs are deliberately excluded.

The full article body is fetched only when the token differs from the durable
SQLite cursor. The body is rendered as current-title H1 followed by YouTrack
Markdown and attachment metadata. Empty or null content still produces the H1,
so clearing an article can never leave stale body bytes in IronRAG.

Routing is selected by ConnectorTemplate before the body fetch. The adapter
therefore compares the fetched article's token and routing facts with the stable
snapshot ref. If either changed, or the article moved outside project filters,
the fetch is deferred until the next stable sweep instead of writing fresh data
to a stale route.

## Deletion and visibility

The YouTrack API exposes neither tombstones nor an article activity cursor.
After a complete stable snapshot, ConnectorTemplate compares article IDs with
active documents under `youtrack:article:` and deletes missing documents.

The permanent-token user's visibility therefore defines the mirrored corpus.
Token revocation, guest fallback, malformed JSON, pagination failure, or a
changing validation snapshot aborts enumeration and suppresses the reaper.
When article visibility is intentionally reduced so the connector user can no
longer read it, the article is absent from a valid snapshot and is removed from
IronRAG.

## Attachments

Article attachments have independent visibility and lifecycle. In the current
framework, dependent documents are not enumerated as primary items and cannot
be reaped when removed from a live parent. This connector therefore includes
only attachment metadata in the parent Markdown and change token. Supporting
binary attachments requires a future framework contract that reconciles
dependents while preserving `parent_external_key`.

## Known upstream title limitation

ConnectorTemplate sends `title` on upload, but its replace request and current
IronRAG replace endpoint do not update catalog title metadata. The current
YouTrack summary is always rendered as H1, so retrieval receives the renamed
title even when the existing IronRAG catalog title remains from the first
upload. Fully updating catalog title requires an IronRAG/SDK API extension.
