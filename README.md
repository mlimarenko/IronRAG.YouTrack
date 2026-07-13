# IronRAG ↔ YouTrack Knowledge Base connector

Polls articles visible to a YouTrack permanent-token user and reconciles them
into [IronRAG](https://github.com/mlimarenko/IronRAG). It is built on the
[IronRAG Connector Template](https://github.com/mlimarenko/IronRAG.ConnectorTemplate).

## Behavior

- Full paginated snapshots of `GET /api/articles` using explicit `$top`/`$skip`.
- Two identical validation passes before yielding a snapshot, preventing an
  offset-pagination race from turning concurrent edits into false deletions.
- Permanent-token identity validation before and after snapshot collection.
- Stable identity `youtrack:article:<database-id>`.
- SHA-256 change tokens over article timestamps, title, project, parent, tags,
  reporter, and attachment descriptors.
- Markdown payloads containing the current title as H1, article content, and
  non-secret attachment metadata.
- Project include/exclude filters and routing facts for project, hierarchy,
  tags, and reporter.
- Retry/backoff for 429 and transient 5xx responses; hard failure on auth or
  malformed snapshots.
- Framework-managed SQLite cursor, incremental payload fetch, replace, and
  orphan deletion after a clean snapshot.

YouTrack's standard webhook integration does not emit Knowledge Base article
events, so this connector is poll-only. The REST API also has no article
`updatedAfter` or deletion feed; complete snapshot reconciliation is required.

Binary attachments are not separate IronRAG documents in v0.0.1. Current
ConnectorTemplate versions do not reconcile a dependent that disappears from a
still-live parent. Attachment names, MIME types, and sizes are included in the
article Markdown, and attachment changes advance the article change token.

## Quick start

```bash
cp .env.example .env.local
cp routing.yaml.example routing.yaml
uv sync --all-extras
uv run pytest --cov
uv run youtrack-connector
```

Required configuration:

```env
YOUTRACK_BASE_URL=https://tracker.example.com/youtrack
YOUTRACK_TOKEN=perm:...
IRONRAG_BASE_URL=https://ironrag.example.com
IRONRAG_API_TOKEN=...
ADMIN_BEARER_TOKEN=...
```

The token user needs `Read Project Basic` and `Read Article` for every project
that should be mirrored. Use a dedicated least-privilege account; visibility
changes then naturally remove articles from the next stable snapshot.

Optional project selection accepts exact project database IDs or short names:

```env
YOUTRACK_PROJECTS=DOCS,0-7
YOUTRACK_EXCLUDE_PROJECTS=ARCHIVE
```

`YOUTRACK_BASE_URL` is the browser-facing service root, including any context
path. For `https://host.example/youtrack`, the connector calls
`https://host.example/youtrack/api/...`.

## Routing facts

Each article emits:

| Fact | Value |
|---|---|
| `article_id` | Stable database ID |
| `article_id_readable` | Human-readable project article ID |
| `project_id` | Project database ID |
| `project`, `project_short_name` | Project short name |
| `project_name` | Project display name |
| `parent_article_id` | Parent database ID, if present |
| `parent_article_id_readable` | Parent readable ID, if present |
| `tag` | Sorted tag-name list |
| `reporter_login` | Reporter login, if visible |

## Manual sync and health

```bash
curl http://127.0.0.1:8088/health
curl -X POST http://127.0.0.1:8088/sync/run \
  -H "Authorization: Bearer $ADMIN_BEARER_TOKEN"
```

The response reports `created`, `replaced`, `noop_unchanged`, `reaped`,
`deferred`, and `errors`.

## Docker

The Dockerfile expects the ConnectorTemplate source staged as `framework/`:

```bash
cp -a ../connectortemplate framework
docker build -t ironrag-youtrack:local .
rm -rf framework
```

For deployment, copy `.env.example` to `.env.local`, create `routing.yaml`, then
run `docker compose up -d`.

The compose file defaults to the immutable release tag `0.0.1`. Override it
with `YOUTRACK_CONNECTOR_TAG` when deliberately testing another release.

## Verification

Unit and framework-lifecycle tests cover pagination, retry, auth fail-closed,
stable identity, Markdown rendering, snapshot consistency, create/no-op/update,
cursor persistence, clear-content replacement, deletion, and reaper safety on
partial enumeration. `tests/e2e` adds a real pinned YouTrack Docker instance
and an isolated IronRAG v0.5.9 stack. The full-stack lifecycle runs the built
connector container against both services and verifies worker ingestion,
source revisions, knowledge chunks, search, soft deletion, and SQLite state
across a process restart.

The complete local procedure and cleanup commands are in
[`tests/e2e/README.md`](tests/e2e/README.md).

See [architecture](docs/ARCHITECTURE.md) and the official YouTrack
[Articles API](https://www.jetbrains.com/help/youtrack/devportal/resource-api-articles.html),
[pagination](https://www.jetbrains.com/help/youtrack/devportal/api-concept-pagination.html),
and [permanent-token authentication](https://www.jetbrains.com/help/youtrack/devportal/authentication-with-permanent-token.html).

## License

MIT — see [LICENSE](LICENSE).
