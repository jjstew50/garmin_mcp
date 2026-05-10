"""
Modular MCP Server for Garmin Connect Data
"""

import io
import json
import os
import sys
from contextvars import ContextVar

import requests
from mcp.server.fastmcp import FastMCP

from garth.exc import GarthHTTPError
from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectConnectionError

# Import all modules
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


# ---------------------------------------------------------------------------
# Multi-user routing via ContextVar + proxy
#
# Each incoming SSE connection/message sets _active_client to the right
# Garmin instance for that user. GarminClientProxy forwards every attribute
# access to whichever client is active in the current async context, so all
# existing tool functions work unchanged.
# ---------------------------------------------------------------------------

_active_client: ContextVar[Garmin] = ContextVar("active_garmin_client")


class GarminClientProxy:
    """Transparent proxy that dispatches attribute access to the per-request Garmin client."""

    def __getattr__(self, name: str):
        try:
            client = _active_client.get()
        except LookupError:
            raise RuntimeError(
                "No Garmin client is active for this request. "
                "Check that MCP_USERS or GARMIN_EMAIL/PASSWORD are configured."
            )
        return getattr(client, name)


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


def init_api(email: str | None, password: str | None, tokens_b64: str | None = None) -> Garmin | None:
    """Initialize a Garmin client for one user.

    Priority:
      1. tokens_b64 argument (passed in from MCP_USERS config)
      2. GARMINTOKENS_BASE64_CONTENT env var (single-user hosted mode)
      3. Token files on disk (per-user subdirectory under GARMINTOKENS path)
      4. email + password re-auth (falls back when tokens missing/expired)
    """
    is_cn = os.getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")
    tokenstore_base = os.getenv("GARMINTOKENS") or "~/.garminconnect"
    tokenstore = _user_tokenstore(tokenstore_base, email)

    # 1. Inline base64 tokens (per-user from MCP_USERS, or single-user env var)
    b64 = tokens_b64 or os.getenv("GARMINTOKENS_BASE64_CONTENT")
    if b64:
        try:
            print(f"Trying to login via base64 tokens (email={email})...", file=sys.stderr)
            garmin = Garmin(is_cn=is_cn)
            garmin.garth.loads(b64.strip())
            # Persist to per-user dir so future restarts skip re-auth
            os.makedirs(tokenstore, exist_ok=True)
            garmin.garth.dump(tokenstore)
            print("Login successful via base64 tokens.", file=sys.stderr)
            return garmin
        except Exception as e:
            print(f"Base64 token load failed ({e}), falling back to file/credential auth.", file=sys.stderr)

    # 2. Token files on disk (per-user directory)
    try:
        print(f"Trying token files in '{tokenstore}' (email={email})...", file=sys.stderr)
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            garmin = Garmin(is_cn=is_cn)
            garmin.login(tokenstore)
        finally:
            sys.stderr = old_stderr
        print("Login successful via token files.", file=sys.stderr)
        return garmin
    except (FileNotFoundError, GarthHTTPError, GarminConnectAuthenticationError):
        pass

    # 3. Re-auth with email + password
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
        garmin = Garmin(email=email, password=password, is_cn=is_cn, prompt_mfa=get_mfa)
        garmin.login()
        os.makedirs(tokenstore, exist_ok=True)
        garmin.garth.dump(tokenstore)
        print(f"Tokens saved to '{tokenstore}'.", file=sys.stderr)
        return garmin
    except (FileNotFoundError, GarthHTTPError, GarminConnectAuthenticationError, GarminConnectConnectionError, requests.exceptions.HTTPError, requests.exceptions.RetryError) as err:
        error_msg = str(err)
        print(f"\nAuthentication failed: {error_msg.split(':')[0]}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Multi-user config loader
# ---------------------------------------------------------------------------

def _load_users() -> list[dict] | None:
    """Parse MCP_USERS env var (JSON array).

    Each entry: {"key": "...", "name": "...", "email": "...", "password": "...", "tokens_b64": "..."}
    tokens_b64 is optional — omit if using email/password re-auth.

    Returns None if MCP_USERS is not set (fall back to single-user mode).
    """
    raw = os.environ.get("MCP_USERS")
    if not raw:
        return None
    try:
        users = json.loads(raw)
        if not isinstance(users, list):
            raise ValueError("MCP_USERS must be a JSON array")
        for u in users:
            if "key" not in u:
                raise ValueError(f"Each user entry must have a 'key' field: {u}")
        return users
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: Failed to parse MCP_USERS: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# ASGI middleware: set the active Garmin client per request based on ?key=
# ---------------------------------------------------------------------------

def _build_user_router(client_map: dict[str, Garmin], mcp_app):
    """Raw ASGI middleware that sets the active Garmin client based on the Bearer token.

    OAuth flow issues api_key as the access token, so the Bearer value IS the api_key.
    For unauthenticated paths (/health, OAuth endpoints) we skip routing.
    """
    _UNAUTH_PREFIXES = ("/health", "/.well-known", "/authorize", "/token", "/register", "/revoke")

    async def router(scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")

            # Pass through health + OAuth endpoints without requiring a client
            if any(path.startswith(p) for p in _UNAUTH_PREFIXES):
                await mcp_app(scope, receive, send)
                return

            # Extract Bearer token from Authorization header
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            bearer = auth_header[7:] if auth_header.startswith("Bearer ") else ""

            garmin_client = client_map.get(bearer)
            if garmin_client is not None:
                tok = _active_client.set(garmin_client)
                try:
                    await mcp_app(scope, receive, send)
                finally:
                    _active_client.reset(tok)
            else:
                # Let FastMCP's auth middleware handle the 401 response
                await mcp_app(scope, receive, send)
        else:
            await mcp_app(scope, receive, send)

    return router


# ---------------------------------------------------------------------------
# App builder (shared between single-user and multi-user modes)
# ---------------------------------------------------------------------------

def _configure_and_build_app(garmin_client, auth_server_provider=None, auth=None) -> FastMCP:
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
    app = workout_templates.register_resources(app)
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    users = _load_users()

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

        # Pre-populate all known API keys so OAuth flow works immediately at startup.
        # Garmin clients are added to client_map as background auth completes.
        client_map: dict[str, Garmin] = {}
        if users:
            known_keys = {u["key"] for u in users}
        else:
            known_keys = {k.strip() for k in os.environ.get("MCP_API_KEYS", "default-key").split(",") if k.strip()}

        oauth_provider = GarminOAuthProvider(
            api_keys=known_keys,
            server_url=server_url,
        )

        def _background_auth():
            import time
            # Retry every 60 minutes — conservative to avoid extending Garmin's rate limit window
            RETRY_INTERVAL = 3600

            if users:
                while any(u["key"] not in client_map for u in users):
                    pending = [u for u in users if u["key"] not in client_map]
                    print(f"Background auth: attempting {len(pending)} user(s)...", file=sys.stderr)
                    any_failed = False
                    for u in pending:
                        name = u.get("name", u["key"])
                        client = init_api(u.get("email"), u.get("password"), u.get("tokens_b64"))
                        if client is None:
                            print(f"  ✗ {name} — auth failed.", file=sys.stderr)
                            any_failed = True
                            continue
                        client_map[u["key"]] = client
                        print(f"  ✓ {name} ({u.get('email', 'no email')})", file=sys.stderr)
                    if any_failed:
                        print("Auth incomplete — retrying in 60 minutes.", file=sys.stderr)
                        time.sleep(RETRY_INTERVAL)
            else:
                single_key = next(iter(known_keys))
                single_email = os.environ.get("GARMIN_EMAIL")
                single_password = os.environ.get("GARMIN_PASSWORD")
                while single_key not in client_map:
                    print("Background auth: single-user mode...", file=sys.stderr)
                    client = init_api(single_email, single_password)
                    if client:
                        client_map[single_key] = client
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
        app = _configure_and_build_app(proxy, auth_server_provider=oauth_provider, auth=auth_settings)

        # Register custom routes (excluded from bearer auth requirement by FastMCP)
        form_handler = oauth_provider.make_form_handler()
        app.custom_route("/authorize-form", methods=["GET", "POST"])(form_handler)

        @app.custom_route("/health", methods=["GET"])
        async def health_check(request: Request) -> Response:
            return PlainTextResponse("ok")

        mcp_starlette = app.streamable_http_app()

        # Outer ASGI wrapper: routes requests to the right Garmin client via ContextVar
        asgi_app = _build_user_router(client_map, mcp_starlette)

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
        app = _configure_and_build_app(garmin_client)
        app.run()


if __name__ == "__main__":
    main()
