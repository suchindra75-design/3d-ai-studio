# Contributing guide

Thanks for helping build AI 3D Studio. This guide covers how the code is
organised, the checks every change must pass, and the project's ground rules.
Setup instructions are in [INSTALLATION.md](INSTALLATION.md).

## Ground rules

1. **Honesty over polish.** No placeholder buttons, simulated progress or mock
   output presented as real. Don't claim measured accuracy for inferred
   geometry. If something is not implemented, the UI doesn't offer it.
2. **Local-first and private.** Never upload user images without the project
   allowing it *and* explicit consent. Strip EXIF/GPS from copies sent to
   providers. Never log or return secrets.
3. **No monetisation code.** No billing, credits, paywalls or telemetry.
4. **Licences matter.** Do not add model weights or assets to the repository.
   New dependencies need a compatible licence; note non-permissive ones
   (e.g. nvdiffrast is non-commercial) in the PR and in `docs/AI_PIPELINE.md`.
5. **Keep layers separate.** `api → services → (database, storage, processing,
   pipelines → providers)`. Services don't import FastAPI; providers don't
   touch the database; screens don't call `fetch` directly.

## Repository map

| Path | What lives there |
|---|---|
| `backend/ai3d` | FastAPI backend (see `docs/ARCHITECTURE.md` §4) |
| `local-engine/ai3d_engine` | Hardware detection, restricted tool runner, reconstruction runners |
| `packages/studio` | Screens shared by web and desktop, query hooks, stores |
| `packages/ui` | Design tokens and UI primitives |
| `packages/api-client` | Typed HTTP client |
| `packages/shared-types` | **Generated** — run `pnpm gen:api`, never edit by hand |
| `apps/web` | Next.js routes + landing page |
| `apps/desktop` | Vite entry + Tauri (`src-tauri/`) |
| `e2e` | Playwright tests (start their own servers) |

## Checks (run before every PR)

```bash
pnpm check                      # eslint, prettier --check, tsc (all packages), vitest
uv run ruff check backend local-engine && uv run ruff format --check backend local-engine
uv run mypy                     # strict
uv run pytest                   # backend + engine
pnpm test:e2e                   # Playwright (desktop + mobile viewports)
pnpm gen:api --check            # generated API types are current
(cd apps/desktop/src-tauri && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test)
```

Optional: run the backend suite against PostgreSQL —

```bash
docker run -d --rm --name ai3d-pg -e POSTGRES_USER=ai3d -e POSTGRES_PASSWORD=ai3d \
  -e POSTGRES_DB=ai3d_test -p 127.0.0.1:55432:5432 postgres:17-alpine
uv sync --all-packages --extra postgres
AI3D_TEST_DATABASE_URL=postgresql+psycopg://ai3d:ai3d@127.0.0.1:55432/ai3d_test uv run pytest
```

## Making common changes

**Change the API.** Edit Pydantic schemas in `backend/ai3d/schemas`, routes in
`backend/ai3d/api/v1`, logic in `backend/ai3d/services`. Response models
inherit from `Schema`/`APIModel` so always-present fields are typed as
required. Then `pnpm gen:api` and update `packages/api-client`.

**Change the database.** Edit models, then
`cd backend && uv run alembic revision --autogenerate -m "…"`. Review the
revision (SQLite batch mode, cyclic FKs) and run the tests on SQLite and
PostgreSQL. Update `docs/DATABASE_SCHEMA.md`.

**Add a screen.** Build it in `packages/studio/src/screens`, use
`useStudio().navigation.Link` for links, add a route in both
`apps/web/app/(studio)/…` and `apps/desktop/src/main.tsx`, plus a test.

**Run an external tool.** Add it to `ALLOWED_TOOLS` in
`local-engine/ai3d_engine/runtime.py` and build argument lists in code. Never
pass user strings to a shell.

## Style

- Python: ruff (format + lint), strict mypy, type hints everywhere, docstrings
  where intent isn't obvious.
- TypeScript: strict, `noUncheckedIndexedAccess`, inline `type` imports,
  Prettier (100 cols, single quotes).
- UI: use tokens (`bg-surface`, `text-muted-foreground`…), not raw colours;
  every interactive element is keyboard reachable with a visible focus ring
  and an accessible name.
- Commits: imperative mood ("Add capture overlap check"), one logical change.

## Reporting security issues

See [SECURITY.md](../SECURITY.md). Please do not open public issues for
vulnerabilities.
