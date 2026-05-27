"""
Minimal OAuth 2.0 authorization server for the Garmin MCP server.

Flow:
  1. Claude registers dynamically (no pre-shared client ID needed)
  2. Claude redirects user to /authorize → we redirect to /authorize-form
  3. User enters Garmin email + password (and MFA if required)
  4. We authenticate with Garmin, save tokens, look up the user's API key by email
  5. We generate an auth code tied to that API key and redirect back to Claude
  6. Claude exchanges the code for a Bearer token (= the API key)
  7. Claude uses Authorization: Bearer <api_key> on all MCP requests
"""

import base64
import json
import secrets
import time
from typing import Any

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


# ---------------------------------------------------------------------------
# Extended auth code that carries the validated API key through the flow
# ---------------------------------------------------------------------------

class GarminAuthCode(AuthorizationCode):
    api_key: str


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class GarminOAuthProvider(OAuthAuthorizationServerProvider[GarminAuthCode, RefreshToken, AccessToken]):
    def __init__(self, api_keys: set[str], server_url: str,
                 client_map: dict | None = None,
                 tokenstore_base: str = "~/.garminconnect"):
        self._api_keys = api_keys
        self._server_url = server_url.rstrip("/")
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, GarminAuthCode] = {}
        self._client_map = client_map if client_map is not None else {}
        self._tokenstore_base = tokenstore_base
        self._pending_mfa: dict[str, dict] = {}  # session_id → {garmin, result2, api_key, encoded_params, expires}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        data = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "state": params.state,
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        return f"{self._server_url}/authorize-form?p={encoded}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> GarminAuthCode | None:
        ac = self._auth_codes.get(authorization_code)
        if ac and ac.expires_at > time.time():
            return ac
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: GarminAuthCode
    ) -> OAuthToken:
        del self._auth_codes[authorization_code.code]
        return OAuthToken(
            access_token=authorization_code.api_key,
            token_type="bearer",
            expires_in=7776000,  # 90 days
            refresh_token=authorization_code.api_key,
        )

    def _is_valid_key(self, token: str) -> bool:
        if token in self._api_keys:
            return True
        try:
            from garmin_mcp.users_db import get_user_by_key
            return get_user_by_key(token) is not None
        except Exception:
            return False

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        if self._is_valid_key(refresh_token):
            return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=[])
        return None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return OAuthToken(
            access_token=refresh_token.token,
            token_type="bearer",
            expires_in=7776000,
            refresh_token=refresh_token.token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self._is_valid_key(token):
            return AccessToken(token=token, client_id="garmin-user", scopes=[])
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        pass

    def _issue_auth_code(self, api_key: str, data: dict) -> str:
        """Create an auth code tied to the given API key and return the redirect URL."""
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = GarminAuthCode(
            code=code,
            api_key=api_key,
            scopes=[],
            expires_at=time.time() + 600,
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
        )
        return construct_redirect_uri(data["redirect_uri"], code=code, state=data.get("state"))

    def _clean_expired_mfa(self):
        now = time.time()
        for k in [k for k, v in self._pending_mfa.items() if v["expires"] < now]:
            del self._pending_mfa[k]

    def make_form_handler(self):
        """Return a Starlette route handler for the /authorize-form endpoint."""
        provider = self

        async def authorize_form(request: Request) -> Response:
            if request.method == "GET":
                import html as _html
                encoded = request.query_params.get("p", "")
                error = request.query_params.get("error", "")
                error_html = f'<p class="error">{_html.escape(error)}</p>' if error else ""
                html = (_LOGIN_FORM_HTML
                        .replace("__ENCODED__", encoded)
                        .replace("__ERROR__", error_html))
                return HTMLResponse(html)

            # POST: authenticate with Garmin
            import html as _html
            from urllib.parse import quote
            from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

            form = await request.form()
            email = str(form.get("email", "")).strip().lower()
            password = str(form.get("password", ""))
            encoded = str(form.get("params", ""))

            try:
                data = json.loads(base64.urlsafe_b64decode(encoded + "=="))
            except Exception:
                return HTMLResponse("Invalid request", status_code=400)

            if not email or not password:
                err = quote("Email and password are required.")
                return RedirectResponse(f"/authorize-form?p={encoded}&error={err}", status_code=302)

            # Look up user by email — only pre-registered users may connect
            try:
                from garmin_mcp.users_db import get_user_by_email
                user = get_user_by_email(email)
            except Exception:
                user = None

            if user is None:
                err = quote("This email is not registered. Contact the administrator.")
                return RedirectResponse(f"/authorize-form?p={encoded}&error={err}", status_code=302)

            api_key = user["key"]
            name = user.get("name", email)

            # Authenticate with Garmin
            is_cn = __import__("os").getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")
            try:
                garmin = Garmin(email=email, password=password, is_cn=is_cn, return_on_mfa=True)
                result1, result2 = garmin.login()

                if result1 == "needs_mfa":
                    provider._clean_expired_mfa()
                    sid = secrets.token_hex(16)
                    provider._pending_mfa[sid] = {
                        "garmin": garmin, "result2": result2,
                        "api_key": api_key, "email": email, "name": name,
                        "encoded_params": encoded,
                        "expires": time.time() + 300,
                    }
                    return RedirectResponse(f"/authorize-form/mfa?session={sid}", status_code=302)

                # Login succeeded — save tokens and register client
                _complete_oauth_login(garmin, api_key, email, name,
                                      provider._client_map, provider._tokenstore_base)
                redirect_url = provider._issue_auth_code(api_key, data)
                return RedirectResponse(redirect_url, status_code=302)

            except GarminConnectTooManyRequestsError:
                err = quote("Rate limited by Garmin. Please wait 15–60 minutes and try again.")
                return RedirectResponse(f"/authorize-form?p={encoded}&error={err}", status_code=302)
            except GarminConnectAuthenticationError:
                err = quote("Invalid email or password.")
                return RedirectResponse(f"/authorize-form?p={encoded}&error={err}", status_code=302)
            except Exception as e:
                err = quote(str(e).split(":")[0][:120])
                return RedirectResponse(f"/authorize-form?p={encoded}&error={err}", status_code=302)

        return authorize_form

    def make_mfa_handler(self):
        """Return a Starlette route handler for the /authorize-form/mfa endpoint."""
        provider = self

        async def authorize_mfa(request: Request) -> Response:
            import html as _html
            from urllib.parse import quote
            from garminconnect import GarminConnectAuthenticationError

            if request.method == "GET":
                sid = request.query_params.get("session", "")
                error = request.query_params.get("error", "")
                provider._clean_expired_mfa()
                if not sid or sid not in provider._pending_mfa:
                    return HTMLResponse(
                        "<h2>Session expired. Please <a href='/authorize-form'>start over</a>.</h2>",
                        status_code=400,
                    )
                error_html = f'<div class="err">{_html.escape(error)}</div>' if error else ""
                html = (_MFA_FORM_HTML
                        .replace("__SESSION__", _html.escape(sid))
                        .replace("__ERROR__", error_html))
                return HTMLResponse(html)

            # POST: complete MFA
            form = await request.form()
            sid = str(form.get("session", ""))
            code = str(form.get("code", "")).strip()

            sess = provider._pending_mfa.get(sid)
            if not sess or sess["expires"] < time.time():
                provider._pending_mfa.pop(sid, None)
                return RedirectResponse("/authorize-form?error=Session+expired.+Please+try+again.", status_code=302)

            garmin = sess["garmin"]
            api_key, email, name = sess["api_key"], sess["email"], sess["name"]
            encoded = sess["encoded_params"]

            try:
                data = json.loads(base64.urlsafe_b64decode(encoded + "=="))
            except Exception:
                return HTMLResponse("Invalid session data", status_code=400)

            try:
                garmin.resume_login(sess["result2"], code)
            except GarminConnectAuthenticationError:
                err = quote("Invalid or expired code. Please try again.")
                return RedirectResponse(f"/authorize-form/mfa?session={sid}&error={err}", status_code=302)
            except Exception as e:
                err = quote(str(e).split(":")[0][:120])
                return RedirectResponse(f"/authorize-form/mfa?session={sid}&error={err}", status_code=302)

            _complete_oauth_login(garmin, api_key, email, name,
                                  provider._client_map, provider._tokenstore_base)
            del provider._pending_mfa[sid]
            redirect_url = provider._issue_auth_code(api_key, data)
            return RedirectResponse(redirect_url, status_code=302)

        return authorize_mfa


# ---------------------------------------------------------------------------
# Shared login completion helper
# ---------------------------------------------------------------------------

def _complete_oauth_login(garmin, api_key: str, email: str, name: str,
                           client_map: dict, tokenstore_base: str):
    """Save tokens, update DB, and register the Garmin client."""
    import os, sys
    from garmin_mcp.connect import _user_tokenstore, _save_connect_meta
    from garmin_mcp.users_db import update_connected_at

    ts = _user_tokenstore(tokenstore_base, email)
    os.makedirs(ts, exist_ok=True)
    garmin.client.dump(ts)
    _save_connect_meta(tokenstore_base, api_key, email, name)
    update_connected_at(api_key)
    client_map[api_key] = garmin
    print(f"oauth: {name} ({email}) connected.", file=sys.stderr)


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

_CSS = """<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f0f2f5;min-height:100vh;display:flex;
     align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border-radius:16px;padding:40px;width:100%;
      max-width:420px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
h1{font-size:22px;font-weight:700;color:#111;margin-bottom:6px}
.sub{font-size:14px;color:#666;margin-bottom:28px}
.field{margin-bottom:16px}
label{display:block;font-size:13px;font-weight:600;color:#333;margin-bottom:6px}
input[type=email],input[type=password],input[type=text]{
  width:100%;padding:11px 14px;border:1.5px solid #e0e0e0;
  border-radius:10px;font-size:15px;transition:border-color .15s;outline:none}
input:focus{border-color:#0071e3}
.btn{width:100%;padding:13px;background:#0071e3;color:#fff;border:none;
     border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;margin-top:4px}
.btn:hover{background:#005bb5}
.err{background:#fef2f2;color:#991b1b;border:1px solid #fca5a5;
     padding:12px 16px;border-radius:10px;font-size:14px;margin-bottom:20px}
.note{font-size:12px;color:#999;text-align:center;margin-top:20px;line-height:1.6}
</style>"""

_FAVICON = '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyBmaWxsPSIjMTg4MmQ0IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PHBhdGggZD0iTTIyLjAxNyAyMi42N0gxLjk4NGMtLjc3IDAtMS4zODgtLjM4My0xLjY5NC0xLjAwMi0uMzg3LS42MS0uMzg3LTEuMzkgMC0yLjAwMkwxMC4zMDQgMi4zM2MuMzg1LS42MTUgMS4wMDItMSAxLjY5NS0xIC43NyAwIDEuMzg2LjM4NSAxLjY5IDFsMTAuMDIgMTcuMzM2Yy4zODcuNjE3LjM4NyAxLjM5IDAgMi4wMDItLjMxLjY5NS0uOTI3IDEuMDAyLTEuNjkzIDEuMDAyeiIvPjwvc3ZnPg==">'

_LOGIN_FORM_HTML = """<!DOCTYPE html><html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Garmin to Claude</title>""" + _FAVICON + _CSS + """</head><body>
<div class="card">
  <h1>🏃 Connect Garmin to Claude</h1>
  <p class="sub">Sign in with your Garmin Connect account to continue.</p>
  __ERROR__
  <form method="post" action="/authorize-form">
    <input type="hidden" name="params" value="__ENCODED__">
    <div class="field"><label>Garmin Email</label>
      <input type="email" name="email" required placeholder="you@example.com"
             autocomplete="email" autocorrect="off" autocapitalize="none" spellcheck="false" autofocus></div>
    <div class="field"><label>Password</label>
      <input type="password" name="password" required placeholder="••••••••"
             autocomplete="current-password"></div>
    <button class="btn" type="submit">Connect</button>
  </form>
  <p class="note">Your password is used once to obtain OAuth tokens and is never stored.</p>
</div></body></html>"""

_MFA_FORM_HTML = """<!DOCTYPE html><html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Two-Factor Auth</title>""" + _FAVICON + _CSS + """</head><body>
<div class="card">
  <h1>🔐 Two-Factor Authentication</h1>
  <p class="sub">Garmin sent a verification code to your email or phone.</p>
  __ERROR__
  <form method="post" action="/authorize-form/mfa">
    <input type="hidden" name="session" value="__SESSION__">
    <div class="field"><label>Verification Code</label>
      <input type="text" name="code" required placeholder="123456" maxlength="8"
             autocomplete="one-time-code" autofocus
             style="letter-spacing:4px;text-align:center;font-size:22px"></div>
    <button class="btn" type="submit">Verify &amp; Connect</button>
  </form>
</div></body></html>"""
