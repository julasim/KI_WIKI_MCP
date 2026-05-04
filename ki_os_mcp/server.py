"""KI-OS MCP Server — Streamable-HTTP Transport mit Bearer-Auth.

Phase-1 Tools (6):
  - search_vault         Volltext-Regex über alle .md
  - read_file            Markdown + Frontmatter
  - list_files           Folder-Inhalt
  - create_note          Neue Note in Projekt-Folder
  - create_task          Neuer Task in 02_System/tasks/
  - append_to_daily      An heutige Daily-Note anhängen

Run lokal:
    VAULT_PATH=/path/to/vault MCP_TOKEN=devtoken python -m ki_os_mcp.server

Run via Docker (siehe Dockerfile + docker-compose.yml im stack-repo).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import frontmatter
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ki_os_mcp import vault
from ki_os_mcp.vault import VaultError

# ---------- Setup ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ki-os-mcp")

MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "3002"))

if not MCP_TOKEN:
    log.warning(
        "MCP_TOKEN ist leer — Server läuft OHNE Auth. "
        "Setze MCP_TOKEN für Production!"
    )

# streamable_http_path="/" damit der Mount unter /mcp die Tools direkt
# unter /mcp serviert (nicht unter /mcp/mcp).
mcp = FastMCP("ki-os-vault", streamable_http_path="/")


# ---------- Tools ------------------------------------------------------------


@mcp.tool()
def search_vault(
    query: str,
    scope: str = "",
    case_sensitive: bool = False,
    max_results: int = 50,
    context: int = 1,
) -> dict[str, Any]:
    """Volltext-Regex-Suche durch alle .md-Files im Vault.

    Args:
        query: Regex-Pattern (Beispiel: "matura|abitur" oder "TODO:.*urgent")
        scope: Optional auf Subfolder beschränken (z.B. "01_Projects")
        case_sensitive: Default False
        max_results: Default 50
        context: Anzahl Zeilen vor/nach Treffer (default 1)

    Returns:
        {hits: [{path, line, match, context}, ...], total: N}
    """
    try:
        hits = vault.grep_vault(
            query,
            scope=scope,
            case_sensitive=case_sensitive,
            max_results=max_results,
            context=context,
        )
        return {"hits": hits, "total": len(hits)}
    except VaultError as e:
        return {"error": str(e), "hits": [], "total": 0}


@mcp.tool()
def read_file(path: str) -> dict[str, Any]:
    """Liest eine Datei aus dem Vault. Wenn .md, wird Frontmatter geparst.

    Args:
        path: Rel-Pfad ab Vault-Root (z.B. "10_Life/daily/2026-05-03.md")

    Returns:
        {path, frontmatter: {...}, body: "..."}
        oder bei Nicht-Markdown: {path, raw: "..."}
    """
    try:
        if path.endswith(".md"):
            post = vault.read_post(path)
            return {
                "path": path,
                "frontmatter": dict(post.metadata),
                "body": post.content,
            }
        else:
            return {"path": path, "raw": vault.read_text(path)}
    except VaultError as e:
        return {"error": str(e)}


@mcp.tool()
def list_files(path: str = "") -> dict[str, Any]:
    """Listet Inhalt eines Vault-Folders (nicht rekursiv).

    Args:
        path: Rel-Pfad zum Folder, "" für Vault-Root

    Returns:
        {path, entries: [{kind: 'dir'|'file', name, path, size?, mtime?}, ...]}
    """
    try:
        return {"path": path, "entries": vault.list_dir(path)}
    except VaultError as e:
        return {"error": str(e)}


@mcp.tool()
def create_note(
    title: str,
    project: str | None = None,
    body: str = "",
    tags: list[str] | None = None,
    subpath: str = "notes",
) -> dict[str, Any]:
    """Erstellt eine neue Markdown-Note.

    Args:
        title: Note-Titel (wird auch zu Slug)
        project: Optional Projekt-Slug (z.B. "dachboden-ausbau"). Wenn gesetzt,
            landet die Note in 01_Projects/{project}/{subpath}/{slug}.md.
            Sonst in 02_Wiki/notes/{slug}.md.
        body: Markdown-Body
        tags: Liste von Tags
        subpath: Subfolder im Projekt (default "notes", z.B. auch "meetings")

    Returns:
        {path, id, created}
    """
    try:
        slug = vault.slugify(title)
        if project:
            rel = f"01_Projects/{project}/{subpath}/{slug}.md"
            note_id = f"{project}-{subpath}-{slug}"
        else:
            rel = f"02_Wiki/notes/{slug}.md"
            note_id = f"note-{slug}"

        # Existiert schon?
        try:
            existing = vault.safe_path(rel)
            if existing.exists():
                return {"error": f"Datei existiert bereits: {rel}"}
        except VaultError as e:
            return {"error": str(e)}

        post = frontmatter.Post(
            body,
            **{
                "id": note_id,
                "type": "note",
                "title": title,
                "created": vault.today_iso(),
                "updated": vault.today_iso(),
                "status": "active",
                "tags": tags or [],
                **({"project": project} if project else {}),
            },
        )
        vault.write_post(rel, post)
        return {"path": rel, "id": note_id, "created": vault.today_iso()}
    except VaultError as e:
        return {"error": str(e)}


@mcp.tool()
def create_task(
    title: str,
    project: str | None = None,
    priority: str = "medium",
    due: str | None = None,
    context: str | None = None,
    body: str = "",
) -> dict[str, Any]:
    """Erstellt einen neuen Task in 02_System/tasks/.

    Args:
        title: Task-Titel
        project: Optional Projekt-Slug
        priority: urgent | high | medium | low (default medium)
        due: ISO-Datum (YYYY-MM-DD) oder None
        context: Kontext-Tag wie "@home", "@work", "@phone"
        body: Optional Markdown-Body unter Frontmatter

    Returns:
        {path, id}
    """
    try:
        if priority not in ("urgent", "high", "medium", "low"):
            return {"error": f"Ungültige Priorität: {priority}"}
        slug = vault.slugify(title)
        task_id = f"t-{slug}"
        rel = f"02_System/tasks/{task_id}.md"

        try:
            if vault.safe_path(rel).exists():
                return {"error": f"Task existiert bereits: {rel}"}
        except VaultError as e:
            return {"error": str(e)}

        meta: dict[str, Any] = {
            "id": task_id,
            "type": "task",
            "title": title,
            "status": "open",
            "priority": priority,
            "created": vault.today_iso(),
            "updated": vault.today_iso(),
        }
        if project:
            meta["project"] = project
        if due:
            meta["due"] = due
        if context:
            meta["context"] = context

        post = frontmatter.Post(body, **meta)
        vault.write_post(rel, post)
        return {"path": rel, "id": task_id}
    except VaultError as e:
        return {"error": str(e)}


@mcp.tool()
def append_to_daily(
    text: str,
    section: str = "Notizen & Gedanken",
    date: str | None = None,
) -> dict[str, Any]:
    """Hängt Text an eine Section der Daily-Note an. Erstellt Daily wenn nötig.

    Args:
        text: Markdown-Text (kann mehrere Zeilen)
        section: Welche Section (default "Notizen & Gedanken").
            Andere übliche: "Heute", "Offen / Einsortieren", "Abends"
        date: ISO-Datum (default heute)

    Returns:
        {path, section}
    """
    try:
        rel = vault.append_to_daily(text, section=section, d=date)
        return {"path": rel, "section": section}
    except VaultError as e:
        return {"error": str(e)}


# ---------- Auth Middleware --------------------------------------------------


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validiert `Authorization: Bearer <token>` gegen MCP_TOKEN.

    Ausnahmen:
      - GET /health  (für Docker-healthcheck)
      - leerer MCP_TOKEN deaktiviert Auth (dev-only, Warnung beim Boot)
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not MCP_TOKEN:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"error": "Missing Bearer token"}, status_code=401
            )
        token = auth.removeprefix("Bearer ").strip()
        if token != MCP_TOKEN:
            return JSONResponse(
                {"error": "Invalid token"}, status_code=401
            )
        return await call_next(request)


# ---------- Health Endpoint --------------------------------------------------


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "vault": str(vault.VAULT_PATH),
            "vault_exists": vault.VAULT_PATH.exists(),
            "auth": "enabled" if MCP_TOKEN else "DISABLED",
        }
    )


# ---------- App-Wiring -------------------------------------------------------


def create_app() -> Starlette:
    """Mount MCP's Streamable-HTTP transport on /mcp + health on /health.

    Delegiert den lifespan an das innere MCP-App damit dessen
    session_manager korrekt initialisiert wird (Starlette triggert
    lifespan von gemounteten ASGI-Apps nicht automatisch).
    """
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp_app.router.lifespan_context(app):
            yield

    return Starlette(
        debug=False,
        middleware=[Middleware(BearerAuthMiddleware)],
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Mount("/mcp", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    log.info("KI-OS MCP Server starting")
    log.info("  Vault: %s (exists=%s)", vault.VAULT_PATH, vault.VAULT_PATH.exists())
    log.info("  Bind:  %s:%d", MCP_HOST, MCP_PORT)
    log.info("  Auth:  %s", "enabled" if MCP_TOKEN else "DISABLED (dev mode)")
    uvicorn.run(create_app(), host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    main()
