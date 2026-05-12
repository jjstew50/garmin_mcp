"""
Web-based Garmin Connect authentication flow.

Provides /connect and /connect/mfa routes. Users log in with their Garmin
credentials (and complete MFA if required). On success, OAuth tokens are
written to the Railway volume and the client is added to client_map live.
Credentials are never stored — only the resulting OAuth tokens.
"""

import html
import json
import os
import re
import secrets
import sys
import time
from urllib.parse import quote

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)


# Pending MFA sessions: session_id → {garmin, result2, key, email, name, expires}
_pending_mfa: dict[str, dict] = {}
_MFA_TTL = 300  # seconds before a pending MFA session expires


def _clean_expired():
    now = time.time()
    for k in [k for k, v in _pending_mfa.items() if v["expires"] < now]:
        del _pending_mfa[k]


def load_connect_meta(tokenstore_base: str) -> dict:
    """Load the key→{email, name} mapping written by prior /connect sessions."""
    path = os.path.join(os.path.expanduser(tokenstore_base), "connect_meta.json")
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_connect_meta(tokenstore_base: str, key: str, email: str, name: str):
    path = os.path.join(os.path.expanduser(tokenstore_base), "connect_meta.json")
    try:
        meta = load_connect_meta(tokenstore_base)
        meta[key] = {
            "email": email,
            "name": name,
            "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        os.makedirs(os.path.expanduser(tokenstore_base), exist_ok=True)
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        print(f"connect: could not save connect_meta: {e}", file=sys.stderr)


def _user_tokenstore(tokenstore_base: str, email: str) -> str:
    safe = email.replace("@", "_").replace(".", "_")
    return os.path.join(os.path.expanduser(tokenstore_base), safe)


# ── HTML ─────────────────────────────────────────────────────────────────────

_CSS = """<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f0f2f5;min-height:100vh;display:flex;
     align-items:center;justify-content:center;padding:20px}
.card{background:#fff;border-radius:16px;padding:40px;width:100%;
      max-width:420px;box-shadow:0 4px 24px rgba(0,0,0,.08)}
h1{font-size:22px;font-weight:700;color:#111;margin-bottom:6px}
.sub{font-size:14px;color:#666;margin-bottom:28px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;
       border-radius:20px;font-size:13px;font-weight:500;margin-bottom:20px}
.ok{background:#dcfce7;color:#166534}.off{background:#fef9c3;color:#713f12}
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
.center{text-align:center}.big{font-size:52px;margin-bottom:16px}
</style>"""

_CONNECT_HTML = """\
<!DOCTYPE html><html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Garmin</title>{css}</head><body>
<div class="card">
  <h1>🏃 Connect Garmin</h1>
  <p class="sub">Sign in with your Garmin Connect account</p>
  {status_html}
  {error_html}
  <form method="post" action="/connect">
    <input type="hidden" name="key" value="{key}">
    <div class="field"><label>Garmin Email</label>
        <input type="email" name="email" required placeholder="you@example.com" value="{email}"
             autocomplete="email" autocorrect="off" autocapitalize="none" spellcheck="false"></div>
    <div class="field"><label>Password</label>
      <input type="password" name="password" required placeholder="••••••••"
             autocomplete="current-password"></div>
    <button class="btn" type="submit">{btn}</button>
  </form>
  <p class="note">Your password is used once to obtain OAuth tokens and is never stored.</p>
</div></body></html>"""

_MFA_HTML = """\
<!DOCTYPE html><html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Two-Factor Auth</title>{css}</head><body>
<div class="card">
  <h1>🔐 Two-Factor Authentication</h1>
  <p class="sub">Garmin sent a verification code to your email or phone.</p>
  {error_html}
  <form method="post" action="/connect/mfa">
    <input type="hidden" name="session" value="{session}">
    <div class="field"><label>Verification Code</label>
      <input type="text" name="code" required placeholder="123456" maxlength="8"
             autocomplete="one-time-code" autofocus
             style="letter-spacing:4px;text-align:center;font-size:22px"></div>
    <button class="btn" type="submit">Verify &amp; Connect</button>
  </form>
</div></body></html>"""

_SUCCESS_HTML = """\
<!DOCTYPE html><html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connected!</title>{css}
<style>
.close-btn{{display:block;width:100%;max-width:220px;margin:20px auto 0;
  padding:11px;background:#166534;color:#fff;border:none;border-radius:10px;
  font-size:15px;font-weight:600;cursor:pointer;text-align:center}}
.close-btn:hover{{background:#14532d}}
</style>
</head><body>
<div class="card center">
  <div class="big">✅</div>
  <h1>Connected!</h1>
  <p class="sub" style="margin-top:8px">Linked as <strong>{email}</strong></p>
  <br>
  <span class="badge ok">🟢 {name} is connected</span>
  <p class="note">Garmin tools are now active in Claude.</p>
  <button class="close-btn" onclick="window.close()">Close Window</button>
</div></body></html>"""


def register_routes(app, client_map: dict, server_url: str, tokenstore_base: str):
    """Register /connect and /connect/mfa routes on the FastMCP app."""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse
    from garmin_mcp.users_db import get_user_by_key, update_connected_at

    is_cn = os.getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")

    async def connect_get(request: Request) -> HTMLResponse:
        key = request.query_params.get("key", "")
        error = request.query_params.get("error", "")

        if not key:
            return HTMLResponse("<h2>Missing ?key= parameter.</h2>", status_code=400)
        user = get_user_by_key(key)
        if user is None:
            return HTMLResponse("<h2>Invalid key.</h2>", status_code=403)

        name = html.escape(user.get("name", key))
        meta = load_connect_meta(tokenstore_base)
        # Pre-fill only from a prior successful connect — not from the DB,
        # since the email is a security factor the user must supply themselves.
        existing_email = html.escape(meta.get(key, {}).get("email", ""))

        if key in client_map:
            status_html = f'<div class="badge ok">🟢 {name} is connected</div>'
            btn = "Reconnect"
        else:
            status_html = f'<div class="badge off">🔴 {name} is not connected</div>'
            btn = "Connect"

        error_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
        return HTMLResponse(_CONNECT_HTML.format(
            css=_CSS, key=html.escape(key), email=existing_email,
            status_html=status_html, error_html=error_html, btn=btn,
        ))

    async def connect_post(request: Request):
        form = await request.form()
        key = str(form.get("key", ""))
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))

        user = get_user_by_key(key)
        if user is None:
            return HTMLResponse("<h2>Invalid key.</h2>", status_code=403)
        if not email or not password:
            return RedirectResponse(
                f"/connect?key={key}&error=Email+and+password+are+required.", status_code=302
            )

        if not _EMAIL_RE.match(email):
            return RedirectResponse(
                f"/connect?key={key}&error=Please+enter+a+valid+email+address.", status_code=302
            )

        # Email in MCP_USERS is required — no email means the key is not authorized to connect.
        expected_email = user.get("email", "").strip().lower()
        if not expected_email:
            err = "This+key+has+no+authorized+email+configured.+Contact+the+administrator."
            return RedirectResponse(f"/connect?key={key}&error={err}", status_code=302)
        if email != expected_email:
            err = "Email+does+not+match+the+account+for+this+key."
            return RedirectResponse(f"/connect?key={key}&error={err}", status_code=302)

        name = user.get("name", key)
        _clean_expired()

        try:
            garmin = Garmin(email=email, password=password, is_cn=is_cn, return_on_mfa=True)
            result1, result2 = garmin.login()

            if result1 == "needs_mfa":
                sid = secrets.token_hex(16)
                _pending_mfa[sid] = {
                    "garmin": garmin, "result2": result2,
                    "key": key, "email": email, "name": name,
                    "expires": time.time() + _MFA_TTL,
                }
                return RedirectResponse(f"/connect/mfa?session={sid}", status_code=302)

            _complete_login(garmin, key, email, name, client_map, tokenstore_base)
            return HTMLResponse(_SUCCESS_HTML.format(css=_CSS, name=html.escape(name), email=html.escape(email)))

        except GarminConnectTooManyRequestsError:
            err = "Rate+limited+by+Garmin.+Please+wait+15%E2%80%9360+minutes+and+try+again."
            return RedirectResponse(f"/connect?key={key}&error={err}", status_code=302)
        except GarminConnectAuthenticationError:
            err = "Invalid+email+or+password.+Please+try+again."
            return RedirectResponse(f"/connect?key={key}&error={err}", status_code=302)
        except Exception as e:
            err = str(e).split(":")[0].replace(" ", "+")[:120]
            return RedirectResponse(f"/connect?key={key}&error={err}", status_code=302)

    async def mfa_get(request: Request) -> HTMLResponse:
        sid = request.query_params.get("session", "")
        error = request.query_params.get("error", "")
        _clean_expired()
        if not sid or sid not in _pending_mfa:
            return HTMLResponse(
                "<h2>Session expired. Please <a href='/connect'>start over</a>.</h2>",
                status_code=400,
            )
        error_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
        return HTMLResponse(_MFA_HTML.format(css=_CSS, session=html.escape(sid), error_html=error_html))

    async def mfa_post(request: Request):
        form = await request.form()
        sid = str(form.get("session", ""))
        code = str(form.get("code", "")).strip()

        sess = _pending_mfa.get(sid)
        if not sess or sess["expires"] < time.time():
            _pending_mfa.pop(sid, None)
            # Redirect to /connect without a key — user will need to re-enter it
            return RedirectResponse(
                "/connect?error=Verification+session+expired.+Please+sign+in+again.",
                status_code=302,
            )

        garmin = sess["garmin"]
        key, email, name = sess["key"], sess["email"], sess["name"]

        try:
            garmin.resume_login(sess["result2"], code)
        except GarminConnectAuthenticationError:
            err = "Invalid+or+expired+code.+Please+try+again."
            return RedirectResponse(f"/connect/mfa?session={sid}&error={err}", status_code=302)
        except Exception as e:
            err = str(e).split(":")[0].replace(" ", "+")[:120]
            return RedirectResponse(f"/connect/mfa?session={sid}&error={err}", status_code=302)

        _complete_login(garmin, key, email, name, client_map, tokenstore_base)
        del _pending_mfa[sid]
        return HTMLResponse(_SUCCESS_HTML.format(css=_CSS, name=html.escape(name), email=html.escape(email)))

    app.custom_route("/connect", methods=["GET"])(connect_get)
    app.custom_route("/connect", methods=["POST"])(connect_post)
    app.custom_route("/connect/mfa", methods=["GET"])(mfa_get)
    app.custom_route("/connect/mfa", methods=["POST"])(mfa_post)


def _complete_login(garmin: Garmin, key: str, email: str, name: str, client_map: dict, tokenstore_base: str):
    """Persist tokens to volume, record connected_at in DB, and add client to client_map."""
    from garmin_mcp.users_db import update_connected_at
    ts = _user_tokenstore(tokenstore_base, email)
    os.makedirs(ts, exist_ok=True)
    garmin.client.dump(ts)
    _save_connect_meta(tokenstore_base, key, email, name)
    update_connected_at(key)
    client_map[key] = garmin
    print(f"connect: {name} ({email}) connected.", file=sys.stderr)
