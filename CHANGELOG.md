# Changelog

## 0.1.0 — 2026-07-14

- Replaced workspace and library UUID routing with canonical
  `<workspace-slug>/<library-slug>` references resolved by ConnectorTemplate
  0.1.0.
- Expanded `routing.yaml.example` into a complete strict schema reference with
  every YouTrack fact, item kind, key, type, default, match rule, and policy
  enum.
- Extended the full-stack bootstrap contract with workspace and library slugs.
  Generated routing files now contain only the friendly catalog ref, while
  internal IDs remain isolated to IAM and lifecycle assertions.
- Added regression coverage for friendly runtime generation and migrated both
  real-YouTrack lifecycle fixtures to compiled routing targets.
- Documented all environment constraints and retained the successful isolated
  Docker lifecycle coverage for create, update, empty content, delete, restart,
  reaping, worker chunks, and search.
- Rejects webhook-only and combined run modes because YouTrack Knowledge Base
  has no complete lifecycle webhooks; polling is the only safe mode.
- Pinned release builds to ConnectorTemplate 0.1.0 and bumped the package
  version to 0.1.0.

## 0.0.1 — 2026-07-13

- Added a poll-only YouTrack Knowledge Base connector on ConnectorTemplate
  v0.0.11.
- Added permanent-token validation, context-path-safe REST URLs, explicit
  pagination, retry/backoff, and two-pass snapshot consistency checks.
- Added stable database-ID mapping, project filters and routing facts,
  composite change tokens, deterministic Markdown rendering, and safe handling
  of empty content and attachment metadata.
- Added unit, framework lifecycle, and opt-in Docker E2E coverage for create,
  no-op, update, clear, delete, restart, and partial-enumeration safety.
- Added a reproducible full-stack Docker E2E fixture with real YouTrack,
  IronRAG v0.5.9, worker ingestion, scoped credentials, and a deterministic
  local OpenAI-compatible provider. The lifecycle verifies source revisions,
  knowledge chunks, search, soft deletion, and persisted connector state.
- Added identity revalidation, duplicate-page rejection, fetch-time route
  revalidation, a loopback-safe automated YouTrack bootstrap, and guarded live
  fixture cleanup.
- Added release test gates, safe tag validation, a commit-pinned framework
  checkout, and lockfile-based container dependency installation.
