"""Starlette-Routes fuer den OAuth 2.1 Authorization Server.

Endpoints:
  GET  /.well-known/oauth-protected-resource  (RFC 9728)
  GET  /.well-known/oauth-authorization-server (RFC 8414)
  POST /oauth/register                          (RFC 7591 — DCR)
  GET  /oauth/authorize                         (Login + Consent UI)
  POST /oauth/authorize                         (Form-Submit aus Login)
  POST /oauth/token                             (Code/Refresh → Access-Token)
  POST /oauth/revoke                            (RFC 7009)

Alle Routes sind public (keine Bearer-Auth davor), geprotectet via PKCE +
Login-Credentials. Rate-Limit gilt aber via globaler Middleware.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ki_os_mcp import oauth

log = logging.getLogger("ki-os-mcp.oauth_routes")


# ---------- HTML-Templates --------------------------------------------------

LOGIN_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>KI-OS Login</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 420px; margin: 60px auto; padding: 0 20px;
         color: #222; line-height: 1.5; }}
  h1 {{ font-size: 22px; margin-bottom: 8px; }}
  .sub {{ color: #666; font-size: 14px; margin-bottom: 32px; }}
  form {{ display: flex; flex-direction: column; gap: 12px; }}
  label {{ font-size: 13px; color: #333; }}
  input {{ padding: 10px 12px; font-size: 15px; border: 1px solid #ccc;
           border-radius: 6px; }}
  input:focus {{ outline: 2px solid #2563eb; border-color: #2563eb; }}
  .err {{ background: #fee; color: #b00; padding: 10px 12px; border-radius: 6px;
          font-size: 13px; margin-bottom: 16px; }}
  button {{ padding: 11px; font-size: 15px; background: #2563eb; color: white;
            border: 0; border-radius: 6px; cursor: pointer; margin-top: 8px; }}
  button:hover {{ background: #1d4ed8; }}
  .client {{ background: #f4f4f5; padding: 12px; border-radius: 6px;
             font-size: 13px; margin-bottom: 20px; }}
  .client b {{ font-weight: 600; }}
  .scopes {{ margin-top: 8px; color: #555; }}
</style>
</head>
<body>
  <h1>KI-OS Vault — Anmeldung</h1>
  <div class="sub">Damit <b>{client_name}</b> auf dein Vault zugreifen darf.</div>

  <div class="client">
    <b>App:</b> {client_name}<br>
    <b>Redirect:</b> <code>{redirect_uri_safe}</code>
    <div class="scopes"><b>Berechtigungen:</b> {scope} (alles — Lese-, Schreib-, Maintain-Operationen)</div>
  </div>

  {error_html}

  <form method="POST" action="/oauth/authorize">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
    <input type="hidden" name="scope" value="{scope}">

    <label for="email">E-Mail</label>
    <input id="email" name="email" type="email" required autofocus
           autocomplete="username" value="{email_value}">

    <label for="password">Passwort</label>
    <input id="password" name="password" type="password" required
           autocomplete="current-password">

    <button type="submit">Anmelden &amp; Autorisieren</button>
  </form>
</body>
</html>"""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _render_login(client_id: str, redirect_uri: str, state: str,
                  code_challenge: str, code_challenge_method: str,
                  scope: str, client_name: str, error: str = "",
                  email_value: str = "") -> str:
    error_html = f'<div class="err">{_esc(error)}</div>' if error else ""
    return LOGIN_HTML.format(
        client_id=_esc(client_id),
        redirect_uri=_esc(redirect_uri),
        redirect_uri_safe=_esc(redirect_uri),
        state=_esc(state),
        code_challenge=_esc(code_challenge),
        code_challenge_method=_esc(code_challenge_method),
        scope=_esc(scope),
        client_name=_esc(client_name),
        error_html=error_html,
        email_value=_esc(email_value),
    )


# ---------- Route-Handlers --------------------------------------------------


async def protected_resource_metadata(_request: Request) -> JSONResponse:
    return JSONResponse(oauth.protected_resource_metadata())


async def authorization_server_metadata(_request: Request) -> JSONResponse:
    return JSONResponse(oauth.authorization_server_metadata())


async def register(request: Request) -> JSONResponse:
    """RFC 7591 — Dynamic Client Registration."""
    if not oauth.is_configured():
        return JSONResponse({"error": "server_not_configured"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request",
                             "error_description": "JSON expected"}, status_code=400)
    client_name = str(data.get("client_name") or "unnamed-client")[:120]
    redirect_uris = data.get("redirect_uris") or []
    try:
        client = await oauth.register_client_async(client_name, redirect_uris)
    except ValueError as e:
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": str(e)}, status_code=400)
    log.info("DCR: registered %s (%s) → %d redirect_uris",
             client["client_id"], client_name, len(redirect_uris))
    return JSONResponse(client, status_code=201)


def _redirect_with_error(redirect_uri: str, state: str, error: str,
                        description: str = "") -> RedirectResponse:
    params = {"error": error, "state": state}
    if description:
        params["error_description"] = description
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


async def authorize_get(request: Request) -> Response:
    """Login-Form rendern. Validiert die OAuth-Params bevor User was sieht."""
    if not oauth.is_configured():
        return HTMLResponse("<h1>OAuth nicht konfiguriert</h1>"
                            "<p>OAUTH_USER_EMAIL, OAUTH_PASSWORD_HASH, OAUTH_JWT_SECRET fehlen.</p>",
                            status_code=503)

    qp = request.query_params
    client_id = qp.get("client_id", "")
    redirect_uri = qp.get("redirect_uri", "")
    response_type = qp.get("response_type", "")
    state = qp.get("state", "")
    code_challenge = qp.get("code_challenge", "")
    code_challenge_method = qp.get("code_challenge_method", "S256")
    scope = qp.get("scope", "admin")

    # Hard-Validation BEVOR Login-Page
    if response_type != "code":
        return HTMLResponse("<h1>unsupported_response_type</h1>"
                            "<p>Nur 'code' supported (Authorization Code Flow).</p>",
                            status_code=400)
    if code_challenge_method != "S256":
        return HTMLResponse("<h1>invalid_request</h1>"
                            "<p>code_challenge_method muss S256 sein (PKCE pflicht).</p>",
                            status_code=400)
    if not code_challenge:
        return HTMLResponse("<h1>invalid_request</h1>"
                            "<p>code_challenge fehlt — PKCE pflicht.</p>",
                            status_code=400)
    client = await oauth.get_client_async(client_id)
    if not client:
        return HTMLResponse(f"<h1>unauthorized_client</h1>"
                            f"<p>Unbekannte client_id <code>{_esc(client_id)}</code>. "
                            f"Erst /oauth/register aufrufen.</p>", status_code=400)
    if not await oauth.validate_redirect_uri_async(client_id, redirect_uri):
        return HTMLResponse("<h1>invalid_redirect_uri</h1>"
                            "<p>redirect_uri stimmt nicht mit der Client-Registrierung ueberein.</p>",
                            status_code=400)

    return HTMLResponse(_render_login(
        client_id=client_id, redirect_uri=redirect_uri, state=state,
        code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        scope=scope, client_name=client["client_name"] or client_id,
    ))


async def authorize_post(request: Request) -> Response:
    """Form-Submit. Verify password → issue auth_code → redirect."""
    form = await request.form()
    client_id = str(form.get("client_id") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    state = str(form.get("state") or "")
    code_challenge = str(form.get("code_challenge") or "")
    code_challenge_method = str(form.get("code_challenge_method") or "S256")
    scope = str(form.get("scope") or "admin")
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")

    # Re-validate alles weil Form könnte manipuliert sein
    client = await oauth.get_client_async(client_id)
    if not client or not await oauth.validate_redirect_uri_async(client_id, redirect_uri):
        return HTMLResponse("<h1>invalid_request</h1>", status_code=400)

    if not await oauth.verify_password_async(email, password):
        # Wieder Login-Form mit Fehler — KEIN Account-Enumerate (gleicher Text fuer
        # falsche Email + falsches Pass)
        return HTMLResponse(_render_login(
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge, code_challenge_method=code_challenge_method,
            scope=scope, client_name=client["client_name"] or client_id,
            error="E-Mail oder Passwort falsch.", email_value=email,
        ), status_code=401)

    # OK — Authorization-Code ausgeben
    code = oauth.issue_auth_code(
        client_id=client_id, redirect_uri=redirect_uri,
        code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        scope=scope, subject=email,
    )
    log.info("OAuth: auth_code issued client=%s subject=%s", client_id, email)

    params = {"code": code, "state": state}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


async def token_endpoint(request: Request) -> JSONResponse:
    """RFC 6749 §3.2 — supports grant_type=authorization_code + refresh_token."""
    if not oauth.is_configured():
        return JSONResponse({"error": "server_not_configured"}, status_code=503)

    form = await request.form()
    grant_type = str(form.get("grant_type") or "")

    if grant_type == "authorization_code":
        return await _grant_authorization_code(form)
    if grant_type == "refresh_token":
        return await _grant_refresh_token(form)
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


async def _grant_authorization_code(form: Any) -> JSONResponse:
    code = str(form.get("code") or "")
    client_id = str(form.get("client_id") or "")
    redirect_uri = str(form.get("redirect_uri") or "")
    code_verifier = str(form.get("code_verifier") or "")
    if not (code and client_id and redirect_uri and code_verifier):
        return JSONResponse({"error": "invalid_request",
                             "error_description": "code/client_id/redirect_uri/code_verifier required"},
                            status_code=400)

    entry = oauth.consume_auth_code(code, client_id, redirect_uri, code_verifier)
    if not entry:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "code abgelaufen, schon eingeloest oder PKCE-Mismatch"},
                            status_code=400)

    access, ttl = oauth.issue_access_token(entry["subject"], client_id, entry["scope"])
    refresh = await oauth.issue_refresh_token_async(client_id, entry["subject"], entry["scope"])
    return JSONResponse({
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ttl,
        "refresh_token": refresh,
        "scope": entry["scope"],
    })


async def _grant_refresh_token(form: Any) -> JSONResponse:
    refresh = str(form.get("refresh_token") or "")
    client_id = str(form.get("client_id") or "")
    if not (refresh and client_id):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    result = await oauth.rotate_refresh_token_async(refresh, client_id)
    if not result:
        return JSONResponse({"error": "invalid_grant",
                             "error_description": "refresh_token abgelaufen, revoked oder replayed"},
                            status_code=400)
    new_access, new_refresh, scope = result
    return JSONResponse({
        "access_token": new_access,
        "token_type": "Bearer",
        "expires_in": oauth.ACCESS_TOKEN_TTL,
        "refresh_token": new_refresh,
        "scope": scope,
    })


async def revoke_endpoint(request: Request) -> Response:
    """RFC 7009 — wir revoken nur refresh_tokens (access-tokens sind kurz-lived JWTs)."""
    if not oauth.is_configured():
        return JSONResponse({"error": "server_not_configured"}, status_code=503)
    form = await request.form()
    token = str(form.get("token") or "")
    if not token:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    await oauth.revoke_refresh_token_async(token)
    # Spec: immer 200 OK auch bei unbekanntem Token (keine Token-Enumeration)
    return Response(status_code=200)


# ---------- Routes-Liste — wird in server.create_app() gemounted -----------


def routes() -> list[Route]:
    return [
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"]),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize_get, methods=["GET"]),
        Route("/oauth/authorize", authorize_post, methods=["POST"]),
        Route("/oauth/token", token_endpoint, methods=["POST"]),
        Route("/oauth/revoke", revoke_endpoint, methods=["POST"]),
    ]
