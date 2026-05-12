"""
Modular MCP Server for Garmin Connect Data
"""

import io
import os
import sys
from contextvars import ContextVar

import requests
from mcp.server.fastmcp import FastMCP

from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectConnectionError, GarminConnectTooManyRequestsError

# Import core Garmin modules (always enabled)
from garmin_mcp import activity_management
from garmin_mcp import health_wellness
from garmin_mcp import user_profile
from garmin_mcp import devices
from garmin_mcp import gear_management
from garmin_mcp import weight_management
from garmin_mcp import challenges
from garmin_mcp import training
from garmin_mcp import workouts
from garmin_mcp import workout_templates
from garmin_mcp import data_management
from garmin_mcp import womens_health
from garmin_mcp import nutrition
from garmin_mcp import workout_builders
from garmin_mcp import courses
from garmin_mcp import activity_analysis
from garmin_mcp.context import _active_user_features, _active_user_key

# tracker and training_memory are imported conditionally inside main() after
# MCP_USERS is parsed, so per-user "training_memory" flags can drive the decision.


# ---------------------------------------------------------------------------
# Multi-user routing via ContextVar + proxy
#
# Each incoming SSE connection/message sets _active_client to the right
# Garmin instance for that user. GarminClientProxy forwards every attribute
# access to whichever client is active in the current async context, so all
# existing tool functions work unchanged.
# ---------------------------------------------------------------------------

_active_client: ContextVar[Garmin] = ContextVar("active_garmin_client")
# _active_user_features / _active_user_key are imported from context.py

# Module-level state shared between the proxy, background auth, and connect routes
_client_map: dict[str, Garmin] = {}
_server_url: str = ""


def _is_auth_error(e: Exception) -> bool:
    """True when the exception means the Garmin session token is invalid/expired."""
    if isinstance(e, GarminConnectAuthenticationError):
        return True
    if isinstance(e, GarminConnectConnectionError):
        msg = str(e)
        return "401" in msg or "403" in msg or "Unauthorized" in msg
    return False


class GarminClientProxy:
    """Transparent proxy that routes Garmin API calls to the per-request client.

    When no client is set (user not connected) it raises a RuntimeError with
    the /connect URL so Claude can relay it to the user.  When a live call
    returns an auth error the expired client is removed from _client_map and
    the same helpful error is raised.
    """

    def __getattr__(self, name: str):
        try:
            client = _active_client.get()
        except LookupError:
            key = _active_user_key.get(None)
            url = f"{_server_url}/connect?key={key}" if key and _server_url else "/connect"
            raise RuntimeError(f"Not connected to Garmin. Please authenticate at: {url}")

        attr = getattr(client, name)
        if not callable(attr):
            return attr

        def _catching(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except Exception as e:
                if _is_auth_error(e):
                    key = _active_user_key.get(None)
                    if key:
                        _client_map.pop(key, None)
                        print(f"connect: session expired ({key[:8]}…), removed from active clients.", file=sys.stderr)
                    url = f"{_server_url}/connect?key={key}" if key and _server_url else "/connect"
                    raise RuntimeError(f"Garmin session expired. Please reconnect at: {url}") from e
                raise

        return _catching


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def get_mfa() -> str:
    if not is_interactive_terminal():
        print(
            "\nERROR: MFA code required but no interactive terminal available.\n"
            "Please run 'garmin-mcp-auth' in your terminal first.\n",
            file=sys.stderr,
        )
        raise RuntimeError("MFA required but non-interactive environment")
    print("\nGarmin Connect MFA required. Please check your email/phone for the code.", file=sys.stderr)
    return input("Enter MFA code: ")


def _user_tokenstore(base: str, email: str | None) -> str:
    """Return a per-user subdirectory under base, keyed by email."""
    if not email:
        return os.path.expanduser(base)
    safe = email.replace("@", "_").replace(".", "_")
    return os.path.join(os.path.expanduser(base), safe)


_GARMIN_TOKEN_FILE = "garmin_tokens.json"


def _seed_tokens_if_missing(
    tokenstore: str,
    garmin_tokens_b64: str | None = None,
) -> bool:
    """Seed garmin_tokens.json to the volume on first boot.

    Per-user tokens (garmin_tokens_b64 from MCP_USERS) are ALWAYS written —
    they represent an explicit configuration update and must take effect on restart.

    The global env var GARMIN_TOKENS (base64 of garmin_tokens.json) is only
    written if the file doesn't already exist, preserving any tokens the library
    has refreshed and written during a prior run.

    Returns True if garmin_tokens.json exists on disk after seeding —
    used by init_api to decide whether to skip email/password fallback.
    """
    import base64

    dest = os.path.join(tokenstore, _GARMIN_TOKEN_FILE)
    env_b64 = os.environ.get("GARMIN_TOKENS", "").strip()

    if garmin_tokens_b64:
        b64 = garmin_tokens_b64  # explicit per-user token — always overwrite
    elif os.path.exists(dest):
        return True  # file already on volume — preserve it
    elif env_b64:
        b64 = env_b64  # first boot with global env var
    else:
        return False

    try:
        decoded = base64.b64decode(b64)
        os.makedirs(tokenstore, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(decoded)
        src = "user config" if garmin_tokens_b64 else "env var (first boot)"
        print(f"Token seed: wrote {_GARMIN_TOKEN_FILE} from {src}.", file=sys.stderr)
    except Exception as e:
        print(f"Token seed: failed to write {_GARMIN_TOKEN_FILE}: {e}", file=sys.stderr)

    return os.path.exists(dest)


def init_api(
    email: str | None,
    password: str | None,
    tokens_b64: str | None = None,
    garmin_tokens_b64: str | None = None,
    # Legacy garth-format fields — ignored, kept for call-site compat
    oauth1_b64: str | None = None,
    oauth2_b64: str | None = None,
) -> Garmin | None:
    """Initialize a Garmin client for one user.

    Priority:
      1. tokens_b64 / garmin_tokens_b64 (base64 of garmin_tokens.json from MCP_USERS)
      2. GARMINTOKENS_BASE64_CONTENT env var (single-user hosted mode)
      3. garmin_tokens.json on disk (per-user subdirectory under GARMINTOKENS path)
         — seeded from garmin_tokens_b64 or GARMIN_TOKENS env var on first boot only
      4. email + password re-auth (only if no token file existed on volume)
    """
    import base64 as _b64mod

    is_cn = os.getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")
    tokenstore_base = os.getenv("GARMINTOKENS") or "~/.garminconnect"
    tokenstore = _user_tokenstore(tokenstore_base, email)

    # Prefer the new field name; fall back to tokens_b64 for compat
    tokens_b64_new = garmin_tokens_b64 or tokens_b64 or os.getenv("GARMINTOKENS_BASE64_CONTENT")

    # Seed garmin_tokens.json only if it doesn't already exist on the volume.
    # Returns True when the file was pre-existing (skip email/password fallback).
    had_volume_tokens = _seed_tokens_if_missing(tokenstore, garmin_tokens_b64)

    # 1. Inline base64 garmin_tokens.json
    if tokens_b64_new:
        try:
            print(f"Trying to login via base64 tokens (email={email})...", file=sys.stderr)
            garmin = Garmin(is_cn=is_cn)
            garmin.client.loads(_b64mod.b64decode(tokens_b64_new.strip()).decode())
            # Persist so future restarts load from disk
            garmin.client.dump(tokenstore)
            print("Login successful via base64 tokens.", file=sys.stderr)
            return garmin
        except Exception as e:
            print(f"Base64 token load failed ({e}), falling back to file/credential auth.", file=sys.stderr)

    # 2. garmin_tokens.json on disk
    try:
        print(f"Trying token files in '{tokenstore}' (email={email})...", file=sys.stderr)
        garmin = Garmin(is_cn=is_cn)
        garmin.login(tokenstore)
        print("Login successful via token files.", file=sys.stderr)
        return garmin
    except (FileNotFoundError, GarminConnectAuthenticationError, GarminConnectConnectionError, GarminConnectTooManyRequestsError):
        pass

    # 3. Re-auth with email + password
    # Skipped when volume tokens were present — avoids Garmin's 429 rate limit.
    # If volume tokens failed, update garmin_tokens in MCP_USERS and restart.
    if had_volume_tokens:
        print(
            f"ERROR: Token file exists but auth failed for {email}. "
            "Update garmin_tokens in MCP_USERS (or use /connect to re-authenticate) and restart.",
            file=sys.stderr,
        )
        return None

    if not email or not password:
        if not is_interactive_terminal():
            print(
                "ERROR: No valid tokens and no credentials provided. "
                "Set GARMIN_EMAIL and GARMIN_PASSWORD, or run garmin-mcp-auth first.",
                file=sys.stderr,
            )
            return None

    print(f"Authenticating with Garmin Connect (email={email})...", file=sys.stderr)
    try:
        garmin = Garmin(email=email, password=password, is_cn=is_cn, return_on_mfa=True)
        result1, result2 = garmin.login()
        if result1 == "needs_mfa":
            mfa_code = get_mfa()
            garmin.resume_login(result2, mfa_code)
        os.makedirs(tokenstore, exist_ok=True)
        garmin.client.dump(tokenstore)
        print(f"Tokens saved to '{tokenstore}'.", file=sys.stderr)
        return garmin
    except (FileNotFoundError, GarminConnectAuthenticationError, GarminConnectConnectionError, requests.exceptions.HTTPError, requests.exceptions.RetryError) as err:
        error_msg = str(err)
        print(f"\nAuthentication failed: {error_msg.split(':')[0]}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Multi-user config loader
# ---------------------------------------------------------------------------

def _load_users() -> list[dict] | None:
    """Load users from the volume SQLite database.

    On first boot, seeds the DB from MCP_USERS env var if the table is empty
    (backward-compat migration path). Once users are in the DB the env var is
    no longer consulted.

    Returns None if the DB is empty (fall back to single-user mode).
    """
    import json
    from garmin_mcp.users_db import init_db, list_users, seed_from_env_users

    init_db()
    users = list_users()
    if not users:
        # First boot: seed from MCP_USERS env var if set
        raw = os.environ.get("MCP_USERS", "").strip()
        if raw:
            try:
                env_users = json.loads(raw)
                n = seed_from_env_users(env_users)
                if n:
                    print(f"DB: seeded {n} user(s) from MCP_USERS.", file=sys.stderr)
                users = list_users()
            except Exception as e:
                print(f"DB: failed to seed from MCP_USERS: {e}", file=sys.stderr)
    return users if users else None


# ---------------------------------------------------------------------------
# ASGI middleware: set the active Garmin client per request based on ?key=
# ---------------------------------------------------------------------------

def _build_user_router(client_map: dict[str, Garmin], features_map: dict[str, dict], mcp_app):
    """Raw ASGI middleware that sets the active Garmin client based on the Bearer token.

    OAuth flow issues api_key as the access token, so the Bearer value IS the api_key.
    For unauthenticated paths (/health, OAuth endpoints) we skip routing.
    Also sets _active_user_features so per-user feature flags are available in tools.
    """
    _UNAUTH_PREFIXES = (
        "/health", "/.well-known", "/authorize", "/token",
        "/register", "/revoke", "/connect", "/authorize-form",
    )

    async def router(scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")

            # Pass through health, OAuth, and connect endpoints without a client
            if any(path.startswith(p) for p in _UNAUTH_PREFIXES):
                await mcp_app(scope, receive, send)
                return

            # Extract Bearer token from Authorization header
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            bearer = auth_header[7:] if auth_header.startswith("Bearer ") else ""

            garmin_client = client_map.get(bearer)
            if garmin_client is not None:
                tok1 = _active_client.set(garmin_client)
                tok2 = _active_user_features.set(features_map.get(bearer, {}))
                tok3 = _active_user_key.set(bearer)
                try:
                    await mcp_app(scope, receive, send)
                finally:
                    _active_client.reset(tok1)
                    _active_user_features.reset(tok2)
                    _active_user_key.reset(tok3)
            else:
                # No Garmin client for this bearer token (not yet connected, or
                # session expired and removed from client_map). Still set
                # _active_user_key so the garmin_connection_status tool can report
                # the reconnect URL even when the Garmin session is dead.
                if bearer:
                    tok3 = _active_user_key.set(bearer)
                    try:
                        await mcp_app(scope, receive, send)
                    finally:
                        _active_user_key.reset(tok3)
                else:
                    await mcp_app(scope, receive, send)
        else:
            await mcp_app(scope, receive, send)

    return router


# ---------------------------------------------------------------------------
# App builder (shared between single-user and multi-user modes)
# ---------------------------------------------------------------------------

def _configure_and_build_app(
    garmin_client,
    auth_server_provider=None,
    auth=None,
    tracker=None,
    training_memory=None,
) -> FastMCP:
    """Configure all modules with garmin_client and return a fully registered FastMCP app."""
    activity_management.configure(garmin_client)
    health_wellness.configure(garmin_client)
    user_profile.configure(garmin_client)
    devices.configure(garmin_client)
    gear_management.configure(garmin_client)
    weight_management.configure(garmin_client)
    challenges.configure(garmin_client)
    training.configure(garmin_client)
    workouts.configure(garmin_client)
    data_management.configure(garmin_client)
    womens_health.configure(garmin_client)
    nutrition.configure(garmin_client)
    workout_builders.configure(garmin_client)
    courses.configure(garmin_client)
    activity_analysis.configure(garmin_client)

    if tracker:
        tracker.configure(garmin_client)
    if training_memory:
        training_memory.configure(garmin_client)

    app = FastMCP("Garmin Connect v1.0", auth_server_provider=auth_server_provider, auth=auth, streamable_http_path="/sse", host="0.0.0.0")
    app = activity_management.register_tools(app)
    app = health_wellness.register_tools(app)
    app = user_profile.register_tools(app)
    app = devices.register_tools(app)
    app = gear_management.register_tools(app)
    app = weight_management.register_tools(app)
    app = challenges.register_tools(app)
    app = training.register_tools(app)
    app = workouts.register_tools(app)
    app = data_management.register_tools(app)
    app = womens_health.register_tools(app)
    app = nutrition.register_tools(app)
    app = workout_builders.register_tools(app)
    app = courses.register_tools(app)
    app = activity_analysis.register_tools(app)

    if tracker:
        app = tracker.register_tools(app)
    if training_memory:
        app = training_memory.register_tools(app)

    app = workout_templates.register_resources(app)

    @app.tool()
    async def garmin_connection_status() -> str:
        """Check whether the Garmin account is currently connected to this server.

        Call this whenever any Garmin tool returns an error — especially
        'session expired' or 'not connected' errors. Returns the current
        connection state and, if disconnected, a URL the user must visit
        in their browser to re-link their Garmin account.
        """
        import json
        key = _active_user_key.get(None)

        if key is None:
            # stdio / single-user mode — if we got here the client is connected
            return json.dumps({"connected": True, "mode": "local"})

        if key in _client_map:
            return json.dumps({"connected": True})

        reconnect_url = f"{_server_url}/connect?key={key}" if _server_url else None
        return json.dumps({
            "connected": False,
            "reconnect_url": reconnect_url,
            "message": (
                "Your Garmin session has expired. "
                "Ask the user to open the reconnect_url in their browser, "
                "sign in with their Garmin credentials, and complete any "
                "two-factor authentication prompt. Once done, Garmin tools "
                "will work again without restarting Claude."
            ),
        })

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    users = _load_users()

    # Determine which optional modules to load — driven by per-user flags OR global env vars.
    _any_training_memory = (
        os.environ.get("GARMIN_TRAINING_MEMORY", "false").lower() in ("true", "1", "yes")
        or any(u.get("training_memory") for u in (users or []))
    )
    _any_tracker = _any_training_memory or (
        os.environ.get("GARMIN_TRACKER_ENABLED", "false").lower() in ("true", "1", "yes")
    )

    _tracker_mod = None
    _training_memory_mod = None
    if _any_tracker:
        from garmin_mcp import tracker as _tracker_mod  # type: ignore[assignment]
    if _any_training_memory:
        from garmin_mcp import training_memory as _training_memory_mod  # type: ignore[assignment]

    if transport == "sse":
        import anyio
        import uvicorn
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse, Response

        from garmin_mcp.oauth import GarminOAuthProvider
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        server_url = os.environ.get("MCP_SERVER_URL", "").rstrip("/")
        if not server_url:
            print("ERROR: MCP_SERVER_URL must be set in SSE mode (e.g. https://your-app.railway.app)", file=sys.stderr)
            sys.exit(1)

        global _server_url
        _server_url = server_url

        tokenstore_base = os.getenv("GARMINTOKENS") or "~/.garminconnect"

        # Use the module-level _client_map so GarminClientProxy and connect routes share it.
        _client_map.clear()
        client_map = _client_map

        if users:
            known_keys = {u["key"] for u in users}
            # Per-user feature flags — keyed by API key for O(1) lookup in middleware.
            features_map: dict[str, dict] = {
                u["key"]: {"training_memory": bool(u.get("training_memory"))}
                for u in users
            }
        else:
            known_keys = {k.strip() for k in os.environ.get("MCP_API_KEYS", "default-key").split(",") if k.strip()}
            features_map = {}

        oauth_provider = GarminOAuthProvider(
            api_keys=known_keys,
            server_url=server_url,
            client_map=_client_map,
            tokenstore_base=tokenstore_base,
        )

        def _background_auth():
            import time
            from garmin_mcp.connect import load_connect_meta

            if users:
                # Load the key→email map written by prior /connect sessions so we
                # can find each user's token directory even if email is omitted from MCP_USERS.
                connect_meta = load_connect_meta(tokenstore_base)
                print(f"Background auth: loading tokens for {len(users)} user(s)...", file=sys.stderr)
                for u in users:
                    key = u["key"]
                    name = u.get("name", key)
                    # Resolve email: explicit in MCP_USERS → connect_meta from prior login → None
                    email = u.get("email") or connect_meta.get(key, {}).get("email")
                    client = init_api(
                        email, u.get("password"), u.get("tokens_b64"),
                        garmin_tokens_b64=u.get("garmin_tokens"),
                    )
                    if client is None:
                        print(
                            f"  ✗ {name} — not connected. "
                            f"Authenticate at: {server_url}/connect?key={key}",
                            file=sys.stderr,
                        )
                    else:
                        _client_map[key] = client
                        print(f"  ✓ {name} ({email or 'no email'})", file=sys.stderr)
            else:
                # Single-user fallback: keep retry loop (no connect UI for anon mode)
                single_key = next(iter(known_keys))
                single_email = os.environ.get("GARMIN_EMAIL")
                single_password = os.environ.get("GARMIN_PASSWORD")
                RETRY_INTERVAL = 3600
                while single_key not in _client_map:
                    print("Background auth: single-user mode...", file=sys.stderr)
                    client = init_api(single_email, single_password)
                    if client:
                        _client_map[single_key] = client
                        print(f"Single-user authenticated. API key: {single_key}", file=sys.stderr)
                    else:
                        print("Auth failed — retrying in 60 minutes.", file=sys.stderr)
                        time.sleep(RETRY_INTERVAL)

        import threading
        threading.Thread(target=_background_auth, daemon=True).start()

        auth_settings = AuthSettings(
            issuer_url=server_url,  # type: ignore[arg-type]
            resource_server_url=None,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

        # All modules share one proxy; the proxy routes to the right client per request
        proxy = GarminClientProxy()
        app = _configure_and_build_app(
            proxy,
            auth_server_provider=oauth_provider,
            auth=auth_settings,
            tracker=_tracker_mod,
            training_memory=_training_memory_mod,
        )

        # Register custom routes (excluded from bearer auth requirement by FastMCP)
        form_handler = oauth_provider.make_form_handler()
        app.custom_route("/authorize-form", methods=["GET", "POST"])(form_handler)
        mfa_handler = oauth_provider.make_mfa_handler()
        app.custom_route("/authorize-form/mfa", methods=["GET", "POST"])(mfa_handler)

        # Garmin Connect web login flow
        from garmin_mcp.connect import register_routes as _register_connect
        _register_connect(app, _client_map, server_url, tokenstore_base)

        @app.custom_route("/health", methods=["GET"])
        async def health_check(request: Request) -> Response:
            return PlainTextResponse("ok")

        @app.custom_route("/status", methods=["GET"])
        async def auth_status(request: Request) -> Response:
            from starlette.responses import JSONResponse
            if users:
                user_status = [
                    {
                        "name": u.get("name", u["key"]),
                        "authenticated": u["key"] in _client_map,
                        "connect_url": f"{server_url}/connect?key={u['key']}",
                    }
                    for u in users
                ]
            else:
                single_key = next(iter(known_keys), None)
                user_status = [{"name": "default", "authenticated": single_key in _client_map}]
            all_ready = all(u["authenticated"] for u in user_status)
            return JSONResponse({"ready": all_ready, "users": user_status})

        mcp_starlette = app.streamable_http_app()

        # Outer ASGI wrapper: routes requests to the right Garmin client via ContextVar
        asgi_app = _build_user_router(client_map, features_map, mcp_starlette)

        port = int(os.environ.get("PORT", 8000))
        config = uvicorn.Config(
            asgi_app, host="0.0.0.0", port=port, log_level="info", http="h11"
        )
        server = uvicorn.Server(config)
        print(f"Starting SSE server on :{port}", file=sys.stderr)
        anyio.run(server.serve)

    else:
        # --- stdio mode (local Claude Desktop use) ---
        email = os.environ.get("GARMIN_EMAIL")
        password = os.environ.get("GARMIN_PASSWORD")

        # Resolve file-based credentials
        email_file = os.environ.get("GARMIN_EMAIL_FILE")
        if email and email_file:
            raise ValueError("Provide only one of GARMIN_EMAIL and GARMIN_EMAIL_FILE")
        elif email_file:
            with open(email_file) as f:
                email = f.read().rstrip()

        password_file = os.environ.get("GARMIN_PASSWORD_FILE")
        if password and password_file:
            raise ValueError("Provide only one of GARMIN_PASSWORD and GARMIN_PASSWORD_FILE")
        elif password_file:
            with open(password_file) as f:
                password = f.read().rstrip()

        garmin_client = init_api(email, password)
        if not garmin_client:
            print("Failed to initialize Garmin Connect client. Exiting.", file=sys.stderr)
            return

        print("Garmin Connect client initialized successfully.", file=sys.stderr)
        app = _configure_and_build_app(garmin_client, tracker=_tracker_mod, training_memory=_training_memory_mod)
        app.run()


if __name__ == "__main__":
    main()
