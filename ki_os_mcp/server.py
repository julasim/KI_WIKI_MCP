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
from mcp.server.transport_security import TransportSecuritySettings
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
#
# transport_security mit deaktivierter DNS-Rebinding-Protection: FastMCP
# enabled das auto-magisch wenn host="127.0.0.1" (default) und blockt dann
# alle Host-Header außer localhost — was uns von extern aussperrt (HTTP 421).
# Unsere Bearer-Auth davor verhindert Rebinding-Angriffe schon (Browser hat
# den Token nicht), darum ist Disable hier sicher.
mcp = FastMCP(
    "ki-os-vault",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


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
    """Erstellt eine neue Markdown-Note (gemäß SCHEMA.md).

    Path-Logik:
      - Generic (kein project): 10_Life/notes/YYYY-MM-DD_<slug>.md
      - Projekt-bezogen:        05_Projects/<project>/<subpath>/YYYY-MM-DD_<slug>.md

    ID = <slug> (ohne Datum-Präfix, ohne note- Prefix — Schema §7).

    Args:
        title: Note-Titel (wird zu Slug)
        project: Optional Projekt-Slug (z.B. "dachboden-ausbau")
        body: Markdown-Body
        tags: Liste von Tags (ohne führendes #)
        subpath: Subfolder im Projekt (default "notes", auch "meetings")

    Returns:
        {path, id, created}
    """
    try:
        slug = vault.slugify(title)
        slug_err = vault.validate_slug(slug)
        if slug_err:
            return {"error": f"Slug ungültig: {slug_err}"}
        date = vault.today_iso()
        filename = f"{date}_{slug}.md"
        if project:
            rel = f"05_Projects/{project}/{subpath}/{filename}"
        else:
            rel = f"10_Life/notes/{filename}"

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
                "id": slug,
                "type": "note",
                "title": title,
                "created": date,
                "updated": date,
                "status": "draft",
                "tags": tags or [],
                **({"project": project} if project else {}),
            },
        )
        vault.write_post(rel, post)
        return {"path": rel, "id": slug, "created": date}
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
    recurrence: str | None = None,
) -> dict[str, Any]:
    """Erstellt einen neuen Task in 10_Life/tasks/ (gemäß SCHEMA.md).

    Tasks bleiben IMMER zentral in 10_Life/tasks/, auch wenn projekt-bezogen
    (Projekt via Frontmatter `project: <slug>`).

    Path:  10_Life/tasks/<slug>.md
    ID:    t-<slug> (Schema §7)

    Args:
        title: Task-Titel (slug max 60 Zeichen, sonst Error)
        project: Optional Projekt-Slug (kommt nur ins Frontmatter, nicht ins Path)
        priority: urgent | high | medium | low (default medium)
        due: ISO-Datum (YYYY-MM-DD) oder None
        context: Kontext-Tag wie "@home", "@work", "@phone"
        body: Optional Markdown-Body unter Frontmatter
        recurrence: daily | weekdays | weekly | monthly (Schema §9, optional)

    Returns:
        {path, id}
    """
    try:
        if priority not in ("urgent", "high", "medium", "low"):
            return {"error": f"Ungültige Priorität: {priority}"}
        if recurrence is not None and recurrence not in ("daily", "weekdays", "weekly", "monthly"):
            return {"error": f"Ungültige recurrence: {recurrence} (erlaubt: daily|weekdays|weekly|monthly)"}
        slug = vault.slugify(title)
        slug_err = vault.validate_slug(slug)
        if slug_err:
            return {"error": f"Slug ungültig: {slug_err}"}
        task_id = f"t-{slug}"
        rel = f"10_Life/tasks/{slug}.md"

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
        if recurrence:
            meta["recurrence"] = recurrence

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


# ---------- Phase 2 Tools ----------------------------------------------------


@mcp.tool()
def edit_file(
    path: str,
    frontmatter_updates: dict[str, Any] | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    """Editiert eine existierende Markdown-Datei.

    Update-Logik:
      - frontmatter_updates: dict-merge in bestehendes Frontmatter
        (None-Werte LÖSCHEN das Feld; sonst überschreiben/hinzufügen)
      - body: wenn nicht None, ersetzt den kompletten Body
      - `updated` wird automatisch auf heute gesetzt

    Args:
        path: Rel-Pfad zur Datei
        frontmatter_updates: dict mit FM-Felder zum mergen (None=Feld löschen)
        body: neuer Body (None = unverändert lassen)

    Returns:
        {path, frontmatter, body_preview} mit dem geupdatetem Stand
    """
    try:
        post = vault.read_post(path)
        if frontmatter_updates:
            for k, v in frontmatter_updates.items():
                if v is None:
                    post.metadata.pop(k, None)
                else:
                    post[k] = v
        if body is not None:
            post.content = body
        vault.write_post(path, post)
        # Re-read für preview
        post2 = vault.read_post(path)
        preview = post2.content[:300] + ("..." if len(post2.content) > 300 else "")
        return {
            "path": path,
            "frontmatter": dict(post2.metadata),
            "body_preview": preview,
        }
    except VaultError as e:
        return {"error": str(e)}


@mcp.tool()
def task(
    id: str,
    action: str,
    due: str | None = None,
    priority: str | None = None,
    body: str | None = None,
    snooze_until: str | None = None,
) -> dict[str, Any]:
    """Statusänderungen + Edits an existierenden Tasks.

    Actions:
      - `done`:   status='done', last_completed=heute (für recurring)
      - `reopen`: status='open', löscht last_completed
      - `snooze`: status='snoozed', setzt due=snooze_until (Pflicht-Param)
      - `edit`:   updated due/priority/body (nur was übergeben wird)

    Args:
        id: Task-ID (`t-foo`) oder slug (`foo`)
        action: done | reopen | snooze | edit
        due: Neue Due-Date (für edit), oder snooze_until-Fallback
        priority: Neue Priority (für edit): urgent|high|medium|low
        body: Neuer Body (für edit)
        snooze_until: ISO-Datum bis wann (für snooze, Pflicht)

    Returns:
        {path, id, status, action_applied}
    """
    try:
        if action not in ("done", "reopen", "snooze", "edit"):
            return {"error": f"Unbekannte action: {action} (erlaubt: done|reopen|snooze|edit)"}

        task_path = vault.find_task(id)
        if not task_path:
            return {"error": f"Task nicht gefunden: {id}"}
        rel = vault.rel_path(task_path)
        post = vault.read_post(rel)

        if action == "done":
            post["status"] = "done"
            post["last_completed"] = vault.today_iso()
        elif action == "reopen":
            post["status"] = "open"
            post.metadata.pop("last_completed", None)
        elif action == "snooze":
            target = snooze_until or due
            if not target:
                return {"error": "snooze braucht snooze_until (oder due) Parameter"}
            post["status"] = "snoozed"
            post["due"] = target
        elif action == "edit":
            if priority is not None:
                if priority not in ("urgent", "high", "medium", "low"):
                    return {"error": f"Ungültige Priorität: {priority}"}
                post["priority"] = priority
            if due is not None:
                post["due"] = due
            if body is not None:
                post.content = body

        vault.write_post(rel, post)
        return {
            "path": rel,
            "id": post.metadata.get("id"),
            "status": post.metadata.get("status"),
            "action_applied": action,
        }
    except VaultError as e:
        return {"error": str(e)}


@mcp.tool()
def create_meeting(
    title: str,
    attendees: list[str],
    project: str | None = None,
    date: str | None = None,
    location: str | None = None,
    duration: str | None = None,
    body: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Erstellt ein Meeting (gemäß SCHEMA.md).

    Path-Logik (analog create_note):
      - Generic:        10_Life/meetings/YYYY-MM-DD_<slug>.md
      - Projekt-bezogen: 05_Projects/<project>/meetings/YYYY-MM-DD_<slug>.md

    Args:
        title: Meeting-Titel
        attendees: Liste der Teilnehmer (Pflicht laut Schema)
        project: Optional Projekt-Slug
        date: ISO-Datum (default heute)
        location: Optional
        duration: Optional, frei (z.B. "60min" oder "1h30")
        body: Markdown-Body (Notizen, Decisions, etc.)
        tags: Optional

    Returns:
        {path, id}
    """
    try:
        if not attendees:
            return {"error": "attendees ist Pflicht (Schema §2: meeting)"}
        d = date or vault.today_iso()
        slug = vault.slugify(title)
        slug_err = vault.validate_slug(slug)
        if slug_err:
            return {"error": f"Slug ungültig: {slug_err}"}
        filename = f"{d}_{slug}.md"
        if project:
            rel = f"05_Projects/{project}/meetings/{filename}"
        else:
            rel = f"10_Life/meetings/{filename}"

        try:
            if vault.safe_path(rel).exists():
                return {"error": f"Meeting existiert bereits: {rel}"}
        except VaultError as e:
            return {"error": str(e)}

        meta: dict[str, Any] = {
            "id": slug,
            "type": "meeting",
            "title": title,
            "date": d,
            "attendees": attendees,
            "status": "draft",
            "created": vault.today_iso(),
            "updated": vault.today_iso(),
            "tags": tags or [],
        }
        if project:
            meta["project"] = project
        if location:
            meta["location"] = location
        if duration:
            meta["duration"] = duration

        post = frontmatter.Post(body, **meta)
        vault.write_post(rel, post)
        return {"path": rel, "id": slug}
    except VaultError as e:
        return {"error": str(e)}


# ---------- Two-Step Delete ---------------------------------------------------
# Pending deletes leben in-memory: Server-Restart cancelt alle pending deletes
# (gewollt — sicheres Default-Verhalten, kein versehentlicher Stale-Delete).

_pending_deletes: dict[str, dict[str, Any]] = {}


@mcp.tool()
def request_delete(path: str, reason: str = "") -> dict[str, Any]:
    """Schritt 1 des Two-Step-Deletes: prüft ob Datei existiert und gibt
    einen Confirmation-Token zurück. Datei wird NOCH NICHT gelöscht.

    Args:
        path: Rel-Pfad der zu löschenden Datei
        reason: Optionaler Grund (für Logging)

    Returns:
        {confirm_token, path, preview, expires_in_seconds}
        Token muss an confirm_delete übergeben werden um Löschung auszuführen.
    """
    try:
        full = vault.safe_path(path)
        if not full.is_file():
            return {"error": f"Datei nicht gefunden: {path}"}
        # Preview: erste 200 Zeichen + Größe
        size = full.stat().st_size
        try:
            preview = full.read_text(encoding="utf-8")[:200].replace("\n", " ⏎ ")
        except OSError:
            preview = "(Binärdatei)"
        # Token = Pfad-Hash + Timestamp (in-memory)
        import secrets

        token = secrets.token_urlsafe(8)
        _pending_deletes[token] = {
            "path": path,
            "requested_at": vault.now_iso(),
            "reason": reason,
        }
        return {
            "confirm_token": token,
            "path": path,
            "size_bytes": size,
            "preview": preview,
            "reason": reason,
            "next_step": f"confirm_delete(token='{token}') ausführen um zu löschen",
        }
    except VaultError as e:
        return {"error": str(e)}


@mcp.tool()
def confirm_delete(token: str) -> dict[str, Any]:
    """Schritt 2 des Two-Step-Deletes: löscht die Datei für die der Token
    via request_delete erstellt wurde.

    Args:
        token: confirm_token aus request_delete

    Returns:
        {deleted: true, path, requested_at}  oder {error}
    """
    pending = _pending_deletes.pop(token, None)
    if not pending:
        return {"error": f"Token unbekannt oder abgelaufen: {token}"}
    try:
        vault.delete_file(pending["path"])
        return {
            "deleted": True,
            "path": pending["path"],
            "requested_at": pending["requested_at"],
            "reason": pending.get("reason", ""),
        }
    except VaultError as e:
        # Bei Fehler Token wieder einsetzen damit Retry möglich ist
        _pending_deletes[token] = pending
        return {"error": str(e)}


# ---------- Move Tool (Wikilink-Migration) -----------------------------------


@mcp.tool()
def move(
    source: str,
    dest: str,
    update_links: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verschiebt/renamed eine Datei und migriert alle Wikilinks im Vault.

    Wenn der Filename (= ID-Slug) sich ändert, werden ALLE [[old-id]],
    [[old-id|display]], [[old-id#anchor]] Referenzen in allen .md-Files auf
    die neue ID umgeschrieben. Auch Frontmatter `related: [old-id]` wird
    migriert. ID im verschobenen File selbst wird auf den neuen Slug gesetzt.

    **Empfehlung:** Erst mit dry_run=True ansehen welche Files betroffen wären.

    Args:
        source: Rel-Pfad der Quelldatei
        dest: Rel-Pfad des Ziels (kann anderen Folder + Filename haben)
        update_links: Wikilinks in anderen Files mit-migrieren (default True)
        dry_run: Nur Preview, nichts schreiben (default False)

    Returns:
        {moved: bool, from, to, id_change?: {old, new},
         wikilinks_updated: [{path, replacements}, ...],
         (dry_run: true falls nur Preview)}
    """
    try:
        src = vault.safe_path(source)
        if not src.is_file():
            return {"error": f"Quelldatei nicht gefunden: {source}"}
        dst = vault.safe_path(dest)
        if dst.exists():
            return {"error": f"Zieldatei existiert bereits: {dest}"}
        if src == dst:
            return {"error": "source und dest sind identisch"}

        # ID-Berechnung nur für .md
        old_id: str | None = None
        new_id: str | None = None
        if src.suffix == ".md" and dst.suffix == ".md":
            old_id = src.stem  # filename ohne .md
            new_id = dst.stem
            # Bei Notes mit Datum-Präfix YYYY-MM-DD_<slug> ist die ID nur der Slug-Teil
            # (Schema §7: note id = slug, Datum nur im Filename).
            # Heuristik: wenn Stem mit YYYY-MM-DD_ anfängt, strip das.
            import re as _re
            date_prefix = _re.compile(r"^\d{4}-\d{2}-\d{2}_")
            if date_prefix.match(old_id):
                old_id = date_prefix.sub("", old_id)
            if date_prefix.match(new_id):
                new_id = date_prefix.sub("", new_id)

        # Wikilink-Refs sammeln
        refs: list[tuple[Any, list[int]]] = []
        if old_id and new_id and old_id != new_id and update_links:
            refs = vault.find_wikilink_refs(old_id)

        refs_summary = [
            {"path": vault.rel_path(p), "lines": lines} for p, lines in refs
        ]

        if dry_run:
            return {
                "dry_run": True,
                "from": source,
                "to": dest,
                "id_change": (
                    {"old": old_id, "new": new_id}
                    if old_id and new_id and old_id != new_id
                    else None
                ),
                "wikilink_refs": refs_summary,
                "would_update_files": len(refs_summary),
            }

        # Echter Move
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".md":
            post = vault.read_post(source)
            if old_id and new_id and old_id != new_id:
                post["id"] = new_id
            # write_post setzt auch updated
            vault.write_post(dest, post)
        else:
            # Binär: einfach kopieren
            dst.write_bytes(src.read_bytes())

        # Source löschen
        src_orig = src
        src.unlink()
        # Leere Eltern-Folder aufräumen
        parent = src_orig.parent
        while (
            parent != vault.VAULT_PATH
            and parent.exists()
            and not any(parent.iterdir())
        ):
            parent.rmdir()
            parent = parent.parent

        # Wikilink-Updates in anderen Files
        updated_files: list[dict[str, Any]] = []
        if old_id and new_id and old_id != new_id and update_links:
            for path, _lines in refs:
                # File evtl. = unsere neue Datei (selbstreferenz)? Skip wenn ja.
                if path == dst:
                    continue
                try:
                    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
                except OSError:
                    continue
                # Body-Wikilinks ersetzen
                new_raw, count = vault.replace_wikilinks(raw, old_id, new_id)
                # FM `related` migrieren
                fm_changed = False
                try:
                    p2 = frontmatter.loads(new_raw)
                    if vault.migrate_id_in_related(p2, old_id, new_id):
                        new_raw = frontmatter.dumps(p2) + "\n"
                        fm_changed = True
                except Exception:  # noqa: BLE001
                    pass
                if count > 0 or fm_changed:
                    path.write_text(new_raw, encoding="utf-8")
                    updated_files.append(
                        {
                            "path": vault.rel_path(path),
                            "wikilink_replacements": count,
                            "frontmatter_related_updated": fm_changed,
                        }
                    )

        return {
            "moved": True,
            "from": source,
            "to": dest,
            "id_change": (
                {"old": old_id, "new": new_id}
                if old_id and new_id and old_id != new_id
                else None
            ),
            "wikilinks_updated": updated_files,
        }
    except VaultError as e:
        return {"error": str(e)}


# ---------- Goal Log ---------------------------------------------------------


@mcp.tool()
def goal_log(
    goal: str,
    text: str,
    subtype: str = "tracker",
    date: str | None = None,
) -> dict[str, Any]:
    """Hängt einen Eintrag an die Tracker-Datei eines Goal-Systems an.

    Path: 10_Life/goals/<goal>/<subtype>.md

    Args:
        goal: Goal-Slug (z.B. "5y-2031")
        text: Markdown-Text (eine Zeile oder mehrere)
        subtype: Subtype-File-Name (default "tracker"; Schema §8 erlaubt
            readme|vision|säulen|routinen|vermögensplan|quartal|monat|woche|tracker)
        date: ISO-Datum als Heading-Präfix (default heute)

    Returns:
        {path, appended}
    """
    try:
        d = date or vault.today_iso()
        rel = f"10_Life/goals/{goal}/{subtype}.md"
        p = vault.safe_path(rel)
        # File anlegen falls nicht da (mit minimalem Skeleton)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            skeleton_fm = {
                "id": f"goal-{goal}-{subtype}",
                "type": "goal-system",
                "subtype": subtype,
                "goal": goal,
                "title": f"{goal} — {subtype}",
                "created": vault.today_iso(),
                "updated": vault.today_iso(),
                "status": "draft",
                "tags": ["goal", goal],
            }
            import yaml as _yaml
            skeleton = (
                "---\n"
                + _yaml.safe_dump(skeleton_fm, allow_unicode=True, sort_keys=True)
                + "---\n\n"
                + f"# {goal} — {subtype}\n\n"
            )
            p.write_text(skeleton, encoding="utf-8")
        # Append
        existing = p.read_text(encoding="utf-8").replace("\r\n", "\n")
        appendix = f"\n## {d}\n{text.strip()}\n"
        p.write_text(existing.rstrip() + appendix, encoding="utf-8")
        return {"path": rel, "appended": appendix.strip().split("\n")[0]}
    except VaultError as e:
        return {"error": str(e)}


# ---------- Project Context --------------------------------------------------


@mcp.tool()
def project_context(
    project: str,
    text: str,
    mode: str = "append",
) -> dict[str, Any]:
    """Update CONTEXT.md eines Projekts (Bot-spezifischer Projekt-Kontext).

    Path: 05_Projects/<project>/CONTEXT.md

    Args:
        project: Projekt-Slug
        text: Neuer Kontext-Text (Markdown)
        mode: "append" (anhängen mit Datum-Header) oder "replace" (Body komplett ersetzen)

    Returns:
        {path, mode}
    """
    try:
        if mode not in ("append", "replace"):
            return {"error": f"mode muss 'append' oder 'replace' sein, nicht {mode}"}
        rel = f"05_Projects/{project}/CONTEXT.md"
        p = vault.safe_path(rel)
        if not p.parent.is_dir():
            return {"error": f"Projekt-Folder nicht gefunden: 05_Projects/{project}/"}
        if mode == "replace" or not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            content = f"# Projekt-Kontext: {project}\n\n_Updated: {vault.today_iso()}_\n\n{text.strip()}\n"
            p.write_text(content, encoding="utf-8")
        else:
            existing = p.read_text(encoding="utf-8").replace("\r\n", "\n")
            appendix = f"\n## {vault.today_iso()}\n{text.strip()}\n"
            p.write_text(existing.rstrip() + appendix, encoding="utf-8")
        return {"path": rel, "mode": mode}
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

    app = Starlette(
        debug=False,
        middleware=[Middleware(BearerAuthMiddleware)],
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            # Mount sowohl auf /mcp als auch /mcp/ damit Clients ohne
            # trailing-slash NICHT auf 307-Redirect laufen (claude.ai folgt
            # dem nicht und meldet "unexpected redirect").
            Mount("/mcp", app=mcp_app),
            Mount("/mcp/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    # Starlette router redirect_slashes default = True — explizit aus,
    # weil wir oben beide Mount-Pfade direkt registrieren.
    app.router.redirect_slashes = False
    return app


def main() -> None:
    log.info("KI-OS MCP Server starting")
    log.info("  Vault: %s (exists=%s)", vault.VAULT_PATH, vault.VAULT_PATH.exists())
    log.info("  Bind:  %s:%d", MCP_HOST, MCP_PORT)
    log.info("  Auth:  %s", "enabled" if MCP_TOKEN else "DISABLED (dev mode)")
    uvicorn.run(create_app(), host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    main()
