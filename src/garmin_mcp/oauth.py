"""
Minimal OAuth 2.0 authorization server for the Garmin MCP server.

Flow:
  1. Claude registers dynamically (no pre-shared client ID needed)
  2. Claude redirects user to /authorize → we redirect to /authorize-form
  3. User enters their API key on the HTML form
  4. We validate the key, generate an auth code, redirect back to Claude
  5. Claude exchanges the code for a Bearer token (= the API key)
  6. Claude uses Authorization: Bearer <api_key> on all MCP requests
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
    def __init__(self, api_keys: set[str], server_url: str):
        self._api_keys = api_keys
        self._server_url = server_url.rstrip("/")
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, GarminAuthCode] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # Encode all OAuth params and redirect to our HTML form
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

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        if refresh_token in self._api_keys:
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
        if token in self._api_keys:
            return AccessToken(token=token, client_id="garmin-user", scopes=[])
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        pass  # tokens are api keys; no revocation needed

    def make_form_handler(self):
        """Return a Starlette route handler for the /authorize-form endpoint."""
        provider = self

        async def authorize_form(request: Request) -> Response:
            if request.method == "GET":
                encoded = request.query_params.get("p", "")
                error = request.query_params.get("error", "")
                error_html = f'<p class="error">{error}</p>' if error else ""
                return HTMLResponse(_FORM_HTML.format(encoded_params=encoded, error_html=error_html))

            # POST: validate key and redirect back to Claude
            form = await request.form()
            api_key = str(form.get("api_key", "")).strip()
            encoded = str(form.get("params", ""))

            try:
                data = json.loads(base64.urlsafe_b64decode(encoded + "=="))
            except Exception:
                return HTMLResponse("Invalid request", status_code=400)

            if api_key not in provider._api_keys:
                from urllib.parse import quote
                return RedirectResponse(
                    f"/authorize-form?p={encoded}&error={quote('Invalid API key — check with Jason')}",
                    status_code=302,
                )

            code = secrets.token_urlsafe(32)
            provider._auth_codes[code] = GarminAuthCode(
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

            redirect_url = construct_redirect_uri(data["redirect_uri"], code=code, state=data.get("state"))
            return RedirectResponse(redirect_url, status_code=302)

        return authorize_form


# ---------------------------------------------------------------------------
# Authorization form HTML
# ---------------------------------------------------------------------------

_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect Garmin to Claude</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 20px;
    }}
    .card {{
      background: white;
      border-radius: 16px;
      padding: 40px 32px;
      max-width: 400px;
      width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,0.08);
    }}
    .logo {{ font-size: 36px; margin-bottom: 12px; }}
    h1 {{ font-size: 22px; font-weight: 700; color: #111; margin-bottom: 8px; }}
    p {{ font-size: 15px; color: #555; margin-bottom: 24px; line-height: 1.5; }}
    input[type=text] {{
      width: 100%;
      padding: 14px 16px;
      font-size: 16px;
      border: 1.5px solid #ddd;
      border-radius: 10px;
      outline: none;
      transition: border-color 0.2s;
    }}
    input[type=text]:focus {{ border-color: #2563eb; }}
    button {{
      width: 100%;
      padding: 14px;
      background: #2563eb;
      color: white;
      border: none;
      border-radius: 10px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      margin-top: 12px;
      transition: background 0.2s;
    }}
    button:hover {{ background: #1d4ed8; }}
    .error {{ color: #dc2626; font-size: 14px; margin-top: 10px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">🏃</div>
    <h1>Connect Garmin to Claude</h1>
    <p>Enter the API key you received to link your Garmin account.</p>
    <form method="post" action="/authorize-form">
      <input type="text" name="api_key" placeholder="Your API key" autocomplete="off" autofocus spellcheck="false">
      <input type="hidden" name="params" value="{encoded_params}">
      {error_html}
      <button type="submit">Connect</button>
    </form>
  </div>
</body>
</html>"""
