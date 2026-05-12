"""
Signup and Garmin connection routes for self-service mode.

Routes:
  GET  /signup                   — email/password or Google signup form
  POST /signup                   — process email/password signup
  GET  /signup/google            — redirect to Google OAuth
  GET  /signup/google/callback   — handle Google callback
  GET  /connect-garmin           — form to link a Garmin account
  POST /connect-garmin           — authenticate Garmin, encrypt & store tokens
  GET  /dashboard                — simple usage dashboard (requires api_key query param)
"""

import io
import os
import secrets
import sys
import time
from typing import Callable
from urllib.parse import urlencode, quote

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from garmin_mcp import auth_db

# ---------------------------------------------------------------------------
# Google OAuth state — in-memory, TTL 10 min, cleared on use (CSRF protection)
# ---------------------------------------------------------------------------

_google_states: dict[str, float] = {}  # state -> expires_at


def _new_google_state() -> str:
    state = secrets.token_urlsafe(24)
    # Prune expired states
    now = time.time()
    expired = [k for k, v in _google_states.items() if v < now]
    for k in expired:
        del _google_states[k]
    _google_states[state] = now + 600
    return state


def _consume_google_state(state: str) -> bool:
    expires = _google_states.pop(state, 0)
    return expires > time.time()


# ---------------------------------------------------------------------------
# Shared CSS / card chrome
# ---------------------------------------------------------------------------

_BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f0f2f5;
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 20px;
}
.card {
  background: white; border-radius: 16px; padding: 40px 36px;
  max-width: 460px; width: 100%;
  box-shadow: 0 4px 32px rgba(0,0,0,0.10);
}
.logo { font-size: 40px; margin-bottom: 12px; }
h1 { font-size: 22px; font-weight: 700; color: #111; margin-bottom: 6px; }
.subtitle { font-size: 15px; color: #666; margin-bottom: 28px; line-height: 1.5; }
label { display: block; font-size: 13px; font-weight: 600; color: #444; margin-bottom: 6px; margin-top: 16px; }
input[type=text], input[type=email], input[type=password] {
  width: 100%; padding: 13px 15px; font-size: 15px;
  border: 1.5px solid #ddd; border-radius: 10px; outline: none;
  transition: border-color 0.15s;
}
input:focus { border-color: #2563eb; }
.btn {
  display: block; width: 100%; padding: 14px; border: none; border-radius: 10px;
  font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 20px;
  transition: background 0.15s; text-align: center; text-decoration: none;
}
.btn-primary { background: #2563eb; color: white; }
.btn-primary:hover { background: #1d4ed8; }
.btn-google {
  background: white; color: #333; border: 1.5px solid #ddd;
  display: flex; align-items: center; justify-content: center; gap: 10px;
}
.btn-google:hover { background: #f8f8f8; }
.divider {
  text-align: center; margin: 20px 0; color: #aaa; font-size: 13px;
  position: relative;
}
.divider::before, .divider::after {
  content: ""; position: absolute; top: 50%; width: 44%; height: 1px;
  background: #e5e5e5;
}
.divider::before { left: 0; }
.divider::after { right: 0; }
.error { background: #fef2f2; border: 1px solid #fca5a5; border-radius: 8px;
         color: #b91c1c; padding: 12px 15px; font-size: 14px; margin-top: 16px; }
.success { background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px;
           color: #166534; padding: 12px 15px; font-size: 14px; margin-top: 16px; }
.api-key-box {
  background: #0f172a; color: #7dd3fc; font-family: monospace; font-size: 14px;
  padding: 16px; border-radius: 10px; word-break: break-all;
  margin: 16px 0; user-select: all; cursor: text;
}
.warning { background: #fffbeb; border: 1px solid #fcd34d; border-radius: 8px;
           color: #92400e; padding: 12px 15px; font-size: 14px; margin: 16px 0; }
.link { color: #2563eb; text-decoration: none; }
.link:hover { text-decoration: underline; }
.small { font-size: 13px; color: #888; margin-top: 16px; }
"""

_GOOGLE_ICON = """<svg width="18" height="18" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
  <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z" fill="#4285F4"/>
  <path d="M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z" fill="#34A853"/>
  <path d="M3.964 10.706c-.18-.54-.282-1.117-.282-1.706s.102-1.166.282-1.706V4.962H.957C.347 6.175 0 7.55 0 9s.348 2.825.957 4.038l3.007-2.332z" fill="#FBBC05"/>
  <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.962L3.964 7.294C4.672 5.167 6.656 3.58 9 3.58z" fill="#EA4335"/>
</svg>"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>{_BASE_CSS}</style>
</head>
<body>
  <div class="card">
    {body}
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Route: GET/POST /signup
# ---------------------------------------------------------------------------

async def signup_get(request: Request) -> Response:
    google_enabled = bool(os.environ.get("GOOGLE_CLIENT_ID"))
    google_btn = f"""
      <a href="/signup/google" class="btn btn-google">{_GOOGLE_ICON} Continue with Google</a>
      <div class="divider">or</div>
    """ if google_enabled else ""
    body = f"""
      <div class="logo">🏃</div>
      <h1>Create your account</h1>
      <p class="subtitle">Sign up to connect Garmin Connect to Claude AI.</p>
      {google_btn}
      <form method="post" action="/signup">
        <label>Email</label>
        <input type="email" name="email" placeholder="you@example.com" required autocomplete="email">
        <label>Password</label>
        <input type="password" name="password" placeholder="At least 8 characters" required autocomplete="new-password">
        <button type="submit" class="btn btn-primary">Create account</button>
      </form>
      <p class="small">Already have an account? <a href="/connect-garmin" class="link">Connect Garmin →</a></p>
    """
    return HTMLResponse(_page("Sign Up — Garmin MCP", body))


async def signup_post(request: Request) -> Response:
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", "")).strip()

    error = ""
    if not email or "@" not in email:
        error = "Please enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    else:
        if await auth_db.email_exists(email):
            error = "An account with that email already exists."

    if error:
        google_enabled = bool(os.environ.get("GOOGLE_CLIENT_ID"))
        google_btn = f"""
          <a href="/signup/google" class="btn btn-google">{_GOOGLE_ICON} Continue with Google</a>
          <div class="divider">or</div>
        """ if google_enabled else ""
        body = f"""
          <div class="logo">🏃</div>
          <h1>Create your account</h1>
          <p class="subtitle">Sign up to connect Garmin Connect to Claude AI.</p>
          {google_btn}
          <div class="error">{error}</div>
          <form method="post" action="/signup">
            <label>Email</label>
            <input type="email" name="email" value="{email}" required autocomplete="email">
            <label>Password</label>
            <input type="password" name="password" required autocomplete="new-password">
            <button type="submit" class="btn btn-primary">Create account</button>
          </form>
        """
        return HTMLResponse(_page("Sign Up — Garmin MCP", body))

    user_id, api_key = await auth_db.create_user(email=email, password=password)
    return _api_key_reveal_page(api_key)


# ---------------------------------------------------------------------------
# Route: GET /signup/google  +  GET /signup/google/callback
# ---------------------------------------------------------------------------

async def signup_google_start(request: Request) -> Response:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    server_url = os.environ.get("MCP_SERVER_URL", "").rstrip("/")
    if not client_id:
        return HTMLResponse(_page("Error", "<p>Google sign-in is not configured.</p>"), status_code=503)

    state = _new_google_state()
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": f"{server_url}/signup/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}", status_code=302)


async def signup_google_callback(request: Request) -> Response:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    server_url = os.environ.get("MCP_SERVER_URL", "").rstrip("/")

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error_param = request.query_params.get("error", "")

    if error_param:
        return _error_page("Google sign-in was cancelled or failed. <a href='/signup' class='link'>Try again</a>.")

    if not _consume_google_state(state):
        return _error_page("Invalid or expired state. <a href='/signup' class='link'>Start over</a>.")

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": f"{server_url}/signup/google/callback",
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()

        # Get user info
        async with httpx.AsyncClient() as client:
            info_resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            info_resp.raise_for_status()
            info = info_resp.json()
    except Exception as e:
        return _error_page(f"Google authentication failed: {e}. <a href='/signup' class='link'>Try again</a>.")

    google_sub = info.get("sub", "")
    email = info.get("email", "")

    if not google_sub:
        return _error_page("Could not retrieve Google account info. <a href='/signup' class='link'>Try again</a>.")

    # Check if user already exists via Google sub
    existing = await auth_db.get_user_by_google_sub(google_sub)
    if existing:
        return _error_page(
            "An account with this Google account already exists. "
            "Visit <a href='/connect-garmin' class='link'>/connect-garmin</a> to use your existing API key."
        )

    # Check if email already registered via password
    if email and await auth_db.email_exists(email):
        return _error_page(
            f"An account with {email} already exists (created with password). "
            "<a href='/signup' class='link'>Sign in with your password instead</a>."
        )

    user_id, api_key = await auth_db.create_user(email=email or None, google_sub=google_sub)
    return _api_key_reveal_page(api_key)


# ---------------------------------------------------------------------------
# Route: GET/POST /connect-garmin
# ---------------------------------------------------------------------------

async def connect_garmin_get(request: Request) -> Response:
    body = f"""
      <div class="logo">🔗</div>
      <h1>Connect your Garmin account</h1>
      <p class="subtitle">
        Enter your API key and Garmin Connect credentials to link your account.
        Your Garmin password is used only to authenticate and is never stored.
      </p>
      <form method="post" action="/connect-garmin">
        <label>Your API key</label>
        <input type="text" name="api_key" placeholder="64-character key from signup"
               autocomplete="off" spellcheck="false" required>
        <label>Garmin email</label>
        <input type="email" name="garmin_email" placeholder="you@example.com" required autocomplete="off">
        <label>Garmin password</label>
        <input type="password" name="garmin_password" required autocomplete="off">
        <button type="submit" class="btn btn-primary">Connect Garmin</button>
      </form>
      <details style="margin-top:24px">
        <summary style="cursor:pointer;font-size:13px;color:#666;font-weight:600">
          Advanced: paste pre-generated token data
        </summary>
        <p style="font-size:13px;color:#888;margin:8px 0">
          Run <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px">garmin-mcp-auth</code>
          locally, then paste the base64 output below (leave Garmin fields blank).
        </p>
        <form method="post" action="/connect-garmin-token">
          <label>API key</label>
          <input type="text" name="api_key" placeholder="64-character key" autocomplete="off" spellcheck="false">
          <label>Token data (base64)</label>
          <input type="text" name="token_b64" placeholder="eyJ0eXBlIjoiT0F1dGgxVG9rZW4i..." autocomplete="off">
          <button type="submit" class="btn btn-primary" style="margin-top:12px">Save token</button>
        </form>
      </details>
    """
    return HTMLResponse(_page("Connect Garmin — Garmin MCP", body))


async def connect_garmin_post(request: Request) -> Response:
    form = await request.form()
    api_key = str(form.get("api_key", "")).strip()
    garmin_email = str(form.get("garmin_email", "")).strip()
    garmin_password = str(form.get("garmin_password", "")).strip()

    if not api_key or not garmin_email or not garmin_password:
        return _error_page("All fields are required. <a href='/connect-garmin' class='link'>Go back</a>.")

    user = await auth_db.lookup_user_by_api_key(api_key)
    if not user:
        return _error_page("API key not recognised. <a href='/connect-garmin' class='link'>Try again</a>.")

    # Authenticate with Garmin (blocking — run in thread via anyio)
    import anyio
    result: dict = {}

    def _do_garmin_auth():
        from garminconnect import Garmin, GarminConnectAuthenticationError
        from garth.exc import GarthHTTPError
        try:
            # Suppress interactive MFA — server-side auth must be non-interactive
            def _no_mfa():
                raise RuntimeError(
                    "Your Garmin account requires MFA. Disable MFA temporarily, "
                    "or use garmin-mcp-auth locally and paste the token above."
                )
            garmin = Garmin(
                email=garmin_email,
                password=garmin_password,
                is_cn=False,
                prompt_mfa=_no_mfa,
            )
            old_stderr = sys.stderr
            sys.stderr = io.StringIO()
            try:
                garmin.login()
            finally:
                sys.stderr = old_stderr
            result["garmin"] = garmin
        except RuntimeError as e:
            result["error"] = str(e)
        except (GarminConnectAuthenticationError, GarthHTTPError) as e:
            result["error"] = f"Garmin authentication failed: {str(e).split(':')[0]}"
        except Exception as e:
            result["error"] = f"Connection error: {str(e).split(':')[0]}"

    await anyio.to_thread.run_sync(_do_garmin_auth)

    if "error" in result:
        return _error_page(f"{result['error']}. <a href='/connect-garmin' class='link'>Try again</a>.")

    garmin = result["garmin"]
    garth_dump = garmin.garth.dumps()

    await auth_db.store_garmin_tokens(user["id"], api_key, garth_dump)

    server_url = os.environ.get("MCP_SERVER_URL", "https://your-server.example.com").rstrip("/")
    body = f"""
      <div class="logo">✅</div>
      <h1>Garmin connected!</h1>
      <p class="subtitle">
        Your Garmin account is now linked. Your tokens are encrypted and stored —
        only your API key can decrypt them.
      </p>
      <p style="font-size:14px;font-weight:600;color:#444;margin-top:20px">Add to Claude Desktop config:</p>
      <div class="api-key-box" style="font-size:12px;line-height:1.6">{{
  "mcpServers": {{
    "garmin": {{
      "url": "{server_url}/sse",
      "headers": {{
        "Authorization": "Bearer {api_key}"
      }}
    }}
  }}
}}</div>
      <p class="small">
        Or in Claude.ai MCP settings, enter:<br>
        <strong>URL:</strong> {server_url}/sse<br>
        <strong>API key:</strong> your {len(api_key)}-character key
      </p>
    """
    return HTMLResponse(_page("Connected — Garmin MCP", body))


async def connect_garmin_token_post(request: Request) -> Response:
    """Advanced: store pre-generated garth token dump (base64)."""
    form = await request.form()
    api_key = str(form.get("api_key", "")).strip()
    token_b64 = str(form.get("token_b64", "")).strip()

    if not api_key or not token_b64:
        return _error_page("Both fields are required. <a href='/connect-garmin' class='link'>Go back</a>.")

    user = await auth_db.lookup_user_by_api_key(api_key)
    if not user:
        return _error_page("API key not recognised. <a href='/connect-garmin' class='link'>Try again</a>.")

    # Validate the dump parses correctly
    import anyio
    result: dict = {}

    def _validate():
        from garminconnect import Garmin
        try:
            g = Garmin(is_cn=False)
            g.garth.loads(token_b64)
            result["dump"] = g.garth.dumps()
        except Exception as e:
            result["error"] = str(e)

    await anyio.to_thread.run_sync(_validate)

    if "error" in result:
        return _error_page(f"Invalid token data: {result['error']}. <a href='/connect-garmin' class='link'>Try again</a>.")

    await auth_db.store_garmin_tokens(user["id"], api_key, result["dump"])

    body = """
      <div class="logo">✅</div>
      <h1>Token saved!</h1>
      <p class="subtitle">
        Your Garmin token has been encrypted and stored.
        You can now use the MCP server with your API key.
      </p>
      <a href="/connect-garmin" class="btn btn-primary">Connect another account</a>
    """
    return HTMLResponse(_page("Token saved — Garmin MCP", body))


# ---------------------------------------------------------------------------
# Route: GET /dashboard
# ---------------------------------------------------------------------------

async def dashboard(request: Request) -> Response:
    api_key = request.query_params.get("api_key", "").strip()
    if not api_key:
        body = """
          <div class="logo">📊</div>
          <h1>Usage Dashboard</h1>
          <form method="get" action="/dashboard">
            <label>API key</label>
            <input type="text" name="api_key" placeholder="Your 64-character API key" required autocomplete="off">
            <button type="submit" class="btn btn-primary">View stats</button>
          </form>
        """
        return HTMLResponse(_page("Dashboard — Garmin MCP", body))

    user = await auth_db.lookup_user_by_api_key(api_key)
    if not user:
        return _error_page("API key not recognised.")

    stats = await auth_db.get_usage_stats(user["id"])
    connected = "✅ Connected" if stats["garmin_connected"] else "❌ Not connected"
    top_tools_html = ""
    for t in stats.get("top_tools", []):
        top_tools_html += f"<tr><td>{t['tool_name'] or 'unknown'}</td><td>{t['count']}</td></tr>"

    body = f"""
      <div class="logo">📊</div>
      <h1>Usage Dashboard</h1>
      <p class="subtitle">Account overview for your Garmin MCP connection.</p>
      <table style="width:100%;border-collapse:collapse;margin-top:20px;font-size:14px">
        <tr><td style="padding:8px 0;color:#666">Garmin status</td><td style="font-weight:600">{connected}</td></tr>
        <tr><td style="padding:8px 0;color:#666">Member since</td><td style="font-weight:600">{(stats.get("member_since") or "—")[:10]}</td></tr>
        <tr><td style="padding:8px 0;color:#666">Total API calls</td><td style="font-weight:600">{stats.get("total_calls", 0)}</td></tr>
        <tr><td style="padding:8px 0;color:#666">Last seen</td><td style="font-weight:600">{(stats.get("last_seen_at") or "never")[:19].replace("T"," ")}</td></tr>
      </table>
      {"<h2 style='font-size:16px;margin-top:24px;margin-bottom:8px'>Top tools</h2><table style='width:100%;border-collapse:collapse;font-size:14px'><tr><th style='text-align:left;padding:4px 0;color:#666'>Tool</th><th style='text-align:left;color:#666'>Calls</th></tr>" + top_tools_html + "</table>" if top_tools_html else ""}
      <p class="small">Reconnect Garmin: <a href="/connect-garmin" class="link">/connect-garmin</a></p>
    """
    return HTMLResponse(_page("Dashboard — Garmin MCP", body))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_key_reveal_page(api_key: str) -> Response:
    """Show the API key exactly once. Never accessible again."""
    body = f"""
      <div class="logo">🔑</div>
      <h1>Your API key</h1>
      <div class="warning">
        <strong>Copy this now.</strong> You will never see it again.
        If you lose it, you'll need to sign up with a new account.
      </div>
      <div class="api-key-box" id="key">{api_key}</div>
      <button class="btn btn-primary" onclick="navigator.clipboard.writeText('{api_key}').then(()=>this.textContent='Copied!')">
        Copy to clipboard
      </button>
      <p style="margin-top:24px;font-size:14px;color:#444">
        <strong>Next step:</strong> Connect your Garmin account so the server can
        access your data on your behalf.
      </p>
      <a href="/connect-garmin" class="btn" style="background:#f0fdf4;color:#166534;border:1.5px solid #86efac;margin-top:12px">
        Connect Garmin →
      </a>
    """
    return HTMLResponse(_page("Your API Key — Garmin MCP", body))


def _error_page(message: str) -> Response:
    body = f"""
      <div class="logo">⚠️</div>
      <h1>Something went wrong</h1>
      <div class="error">{message}</div>
    """
    return HTMLResponse(_page("Error — Garmin MCP", body), status_code=400)


# ---------------------------------------------------------------------------
# Route registration helper
# ---------------------------------------------------------------------------

def register_routes(app, server_url: str) -> None:
    """Attach all signup/connect routes to a FastMCP app."""
    app.custom_route("/signup", methods=["GET"])(signup_get)
    app.custom_route("/signup", methods=["POST"])(signup_post)
    app.custom_route("/signup/google", methods=["GET"])(signup_google_start)
    app.custom_route("/signup/google/callback", methods=["GET"])(signup_google_callback)
    app.custom_route("/connect-garmin", methods=["GET"])(connect_garmin_get)
    app.custom_route("/connect-garmin", methods=["POST"])(connect_garmin_post)
    app.custom_route("/connect-garmin-token", methods=["POST"])(connect_garmin_token_post)
    app.custom_route("/dashboard", methods=["GET"])(dashboard)
