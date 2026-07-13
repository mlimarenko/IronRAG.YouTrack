# IronRAG.YouTrack — developer guide

This repository contains the YouTrack Knowledge Base adapter for the sibling
`IronRAG.ConnectorTemplate` framework. Vendor-specific code lives in
`src/youtrack_connector`; routing, cursor state, reconciliation, and IronRAG
mutations remain framework responsibilities.

## Contract

- Enumerate every article visible to the configured permanent-token user.
- Use the YouTrack database `Article.id` as stable identity and
  `youtrack:article:<id>` as `external_key`.
- Preserve the human-readable `idReadable` only in file names and citation URLs.
- Fail the whole enumeration on auth, pagination, JSON, or snapshot-consistency
  errors. A partial snapshot must never reach the framework reaper.
- Always materialize nullable/empty content as deterministic Markdown so a
  cleared source article replaces old IronRAG bytes.
- Keep the adapter poll-only. Standard YouTrack webhooks do not cover Knowledge
  Base article lifecycle events.
- Do not emit binary attachments as dependent documents until the framework can
  reconcile removal of dependents from a still-live parent. Attachment metadata
  is represented inside the parent article payload and change token.

## Commands

```bash
uv sync --all-extras
uv run pytest --cov
uv run ruff check .
uv run mypy src
```

Live Docker coverage is documented in `tests/e2e/README.md` and is opt-in.
Synthetic fixtures only; no real hostnames, tokens, project names, article
content, workspace IDs, or library IDs belong in this repository.

## Local `.env`

- Check `.env`/`.env.local` in this repository before external secret stores.
- Treat all values as secrets. Never print or commit them; refer to variable
  names only.
