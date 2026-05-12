# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (uses uv)
uv sync

# Run the server locally (stdio mode, requires GARMIN_EMAIL/PASSWORD or existing tokens)
uv run garmin-mcp

# Pre-authenticate and save tokens to disk (run this before non-interactive use)
uv run garmin-mcp-auth

# Run all tests (unit + integration)
uv run pytest tests/integration tests/unit

# Run a single test file
uv run pytest tests/unit/test_token_utils.py -v

# Run tests by marker
uv run pytest -m unit
uv run pytest -m integration
uv run pytest -m e2e   # requires real Garmin credentials

# Build and run via Docker
docker compose up --build
```

## Architecture

### Two transport modes

`MCP_TRANSPORT=stdio` (default) — local Claude Desktop mode. Single Garmin client initialized at startup, passed directly to all modules. Blocks on `app.run()`.

`MCP_TRANSPORT=sse` — hosted/Railway mode. Runs a uvicorn HTTP server on `PORT`. Auth happens in a background thread; clients populate `client_map` as they succeed. The ASGI middleware (`_build_user_router`) sets the correct Garmin client per request via `ContextVar` before dispatching to FastMCP.

### Module pattern

Every feature area is its own file (`activity_management.py`, `health_wellness.py`, etc.). Each module exposes two functions:
- `configure(client)` — stores the garmin client in a module-level global
- `register_tools(app) -> app` — registers `@app.tool()` decorated async functions and returns the app

`__init__.py:main()` calls configure + register_tools for every module in sequence.

### Multi-user routing

In SSE mode, `GarminClientProxy` is the shared garmin client passed to all modules. It delegates every attribute access to `_active_client.get()`, which is a `ContextVar` set by the ASGI middleware from a `Bearer` token lookup in `client_map`. Per-user feature flags (e.g. `training_memory`) travel the same way via `_active_user_features` (defined in `context.py`, imported by modules that need it).

### Optional modules

`tracker` and `training_memory` are only imported when their feature flags are enabled:
- `GARMIN_TRACKER_ENABLED=true` enables `tracker.py` (SQLite-backed activity/health/weight sync + query)
- `GARMIN_TRAINING_MEMORY=true` enables both `tracker.py` and `training_memory.py` (training plans, phases, notes)
- Per-user: set `"training_memory": true` on a user entry in `MCP_USERS`

The SQLite database path defaults to `GARMIN_DATA_PATH` (`/app/data` in Docker).

### OAuth flow (SSE mode)

`GarminOAuthProvider` in `oauth.py` implements MCP's `OAuthAuthorizationServerProvider`. The Bearer token IS the API key — no separate token minting. Flow: Claude dynamic registration → `/authorize` → `/authorize-form` (HTML form) → user pastes API key → auth code → token exchange → `Authorization: Bearer <api_key>` on all requests.

## Key environment variables

| Variable | Purpose |
|---|---|
| `MCP_TRANSPORT` | `stdio` (default) or `sse` |
| `MCP_SERVER_URL` | Required in SSE mode (e.g. `https://your-app.railway.app`) |
| `MCP_USERS` | JSON array of `{key, name, email, password, oauth1_token, oauth2_token, training_memory}` |
| `MCP_API_KEYS` | Comma-separated keys for single-user SSE mode |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Single-user credentials (stdio or single-user SSE) |
| `GARMIN_OAUTH1_TOKEN` / `GARMIN_OAUTH2_TOKEN` | Base64 token files, seeded to volume on first boot only |
| `GARMINTOKENS` | Token storage directory (default: `~/.garminconnect`) |
| `GARMIN_TRAINING_MEMORY` | `true` to enable training memory globally |
| `GARMIN_TRACKER_ENABLED` | `true` to enable tracker without training memory |

`mcp_users.json` in the repo root is the local development equivalent of the `MCP_USERS` env var — load it with `MCP_USERS=$(cat mcp_users.json)`.

## Deployment

Deployed on Railway using `railway.toml`. `Dockerfile` uses `python:3.12-slim` + `uv`. Two Docker volumes: `garmin-tokens` (OAuth token files) and `garmin-data` (SQLite DB). Token files are seeded from `oauth1_token`/`oauth2_token` in `MCP_USERS` on first boot only — existing files are never overwritten to avoid Garmin's 429 rate limits on restart.

## Testing

Tests use pytest-asyncio in `auto` mode. The `create_test_app(module, mock_client)` helper in `conftest.py` wires a mock Garmin client into a module and returns a configured FastMCP app for integration tests. E2E tests (`tests/e2e/`) are skipped by default; run with `pytest -m e2e`.
