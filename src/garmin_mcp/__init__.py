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
from garminconnect import Garmin, GarminConnectAuthenticationError

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


def init_api(email: str | None, password: str | None, tokens_b64: str | None = None) -> Garmin | None:
    """Initialize a Garmin client for one user.

    Priority:
      1. tokens_b64 argument (passed in from MCP_USERS config)
      2. GARMINTOKENS_BASE64_CONTENT env var (single-user hosted mode)
      3. Token files on disk (GARMINTOKENS path)
      4. email + password re-auth (falls back when tokens missing/expired)
    """
    is_cn = os.getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")
    tokenstore = os.getenv("GARMINTOKENS") or "~/.garminconnect"
    tokenstore_base64 = os.getenv("GARMINTOKENS_BASE64") or "~/.garminconnect_base64"

    # 1. Inline base64 tokens (per-user from MCP_USERS, or single-user env var)
    b64 = tokens_b64 or os.getenv("GARMINTOKENS_BASE64_CONTENT")
    if b64:
        try:
            print(f"Trying to login via base64 tokens (email={email})...", file=sys.stderr)
            garmin = Garmin(is_cn=is_cn)
            garmin.garth.loads(b64.strip())
            print("Login successful via base64 tokens.", file=sys.stderr)
            return garmin
        except Exception as e:
            print(f"Base64 token load failed ({e}), falling back to file/credential auth.", file=sys.stderr)

    # 2. Token files on disk
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
        garmin.garth.dump(tokenstore)
        token_base64 = garmin.garth.dumps()
        dir_path = os.path.expanduser(tokenstore_base64)
        with open(dir_path, "w") as f:
            f.write(token_base64)
        print(f"Tokens saved to '{tokenstore}' and '{dir_path}'.", file=sys.stderr)
        return garmin
    except (FileNotFoundError, GarthHTTPError, GarminConnectAuthenticationError, requests.exceptions.HTTPError) as err:
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
    """Raw ASGI middleware that sets the active Garmin client before each request."""
    from urllib.parse import parse_qs

    async def router(scope, receive, send):
        if scope["type"] == "http":
            qs = scope.get("query_string", b"").decode()
            params = parse_qs(qs)
            key = params.get("key", [""])[0]

            if scope["path"] == "/health":
                # Health check bypasses auth
                await mcp_app(scope, receive, send)
                return

            client = client_map.get(key)
            if client is None:
                response_body = b"Unauthorized"
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"content-length", str(len(response_body)).encode())]})
                await send({"type": "http.response.body", "body": response_body})
                return

            token = _active_client.set(client)
            try:
                await mcp_app(scope, receive, send)
            finally:
                _active_client.reset(token)
        else:
            await mcp_app(scope, receive, send)

    return router


# ---------------------------------------------------------------------------
# App builder (shared between single-user and multi-user modes)
# ---------------------------------------------------------------------------

def _configure_and_build_app(garmin_client) -> FastMCP:
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

    app = FastMCP("Garmin Connect v1.0")
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
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Mount, Route

        async def health(request):
            return PlainTextResponse("ok")

        if users:
            # --- Multi-user mode ---
            print(f"Multi-user mode: initializing {len(users)} user(s)...", file=sys.stderr)
            client_map: dict[str, Garmin] = {}
            for u in users:
                name = u.get("name", u["key"])
                client = init_api(u.get("email"), u.get("password"), u.get("tokens_b64"))
                if client is None:
                    print(f"WARNING: Could not authenticate user '{name}' — skipping.", file=sys.stderr)
                    continue
                client_map[u["key"]] = client
                print(f"  ✓ {name} ({u.get('email', 'no email')})", file=sys.stderr)

            if not client_map:
                print("ERROR: No users authenticated. Exiting.", file=sys.stderr)
                sys.exit(1)

            # All modules share one proxy; the proxy routes to the right client per request
            proxy = GarminClientProxy()
            app = _configure_and_build_app(proxy)

            mcp_starlette = app.sse_app()
            combined = Starlette(routes=[
                Route("/health", health),
                Mount("/", mcp_starlette),
            ])
            asgi_app = _build_user_router(client_map, combined)

        else:
            # --- Single-user mode (backward compatible) ---
            email = os.environ.get("GARMIN_EMAIL")
            password = os.environ.get("GARMIN_PASSWORD")
            garmin_client = init_api(email, password)
            if not garmin_client:
                print("Failed to initialize Garmin Connect client. Exiting.", file=sys.stderr)
                sys.exit(1)

            app = _configure_and_build_app(garmin_client)
            mcp_starlette = app.sse_app()
            combined = Starlette(routes=[
                Route("/health", health),
                Mount("/", mcp_starlette),
            ])

            # Simple API key middleware for single-user mode
            api_keys_raw = os.environ.get("MCP_API_KEYS", "")
            if api_keys_raw:
                allowed = {k.strip() for k in api_keys_raw.split(",") if k.strip()}

                # Set the single client as active and check the key
                single_client = garmin_client

                async def single_user_router(scope, receive, send):
                    from urllib.parse import parse_qs
                    if scope["type"] == "http" and scope["path"] != "/health":
                        qs = scope.get("query_string", b"").decode()
                        key = parse_qs(qs).get("key", [""])[0]
                        if key not in allowed:
                            body = b"Unauthorized"
                            await send({"type": "http.response.start", "status": 401,
                                        "headers": [(b"content-type", b"text/plain"),
                                                    (b"content-length", str(len(body)).encode())]})
                            await send({"type": "http.response.body", "body": body})
                            return
                    await combined(scope, receive, send)

                asgi_app = single_user_router
                print(f"Single-user mode, {len(allowed)} API key(s) configured.", file=sys.stderr)
            else:
                print("WARNING: MCP_API_KEYS not set — server is open to anyone!", file=sys.stderr)
                asgi_app = combined

        port = int(os.environ.get("PORT", 8000))
        config = uvicorn.Config(asgi_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config)
        print(f"Starting SSE server on :{port}  →  connect at /sse?key=YOUR_KEY", file=sys.stderr)
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
