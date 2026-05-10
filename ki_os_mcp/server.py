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
import re
import shutil
from contextlib import asynccontextmanager
from typing import Any

import frontmatter
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ki_os_mcp import audit, maintain, oauth, oauth_routes, snapshot, validators, vault
from ki_os_mcp.ratelimit import RateLimitMiddleware
from ki_os_mcp.vault import VaultError

# ---------- Setup ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ki-os-mcp")

MCP_TOKEN = os.environ.get("MCP_TOKEN", "").strip()
# Optional zweiter Token für Rotation (alter bleibt 24h gültig nach Wechsel)
MCP_TOKEN_LEGACY = os.environ.get("MCP_TOKEN_LEGACY", "").strip()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "5002"))

if not MCP_TOKEN:
    log.warning(
        "MCP_TOKEN ist leer — Server läuft OHNE Auth. "
        "Setze MCP_TOKEN für Production!"
    )
if MCP_TOKEN_LEGACY:
    log.info("Legacy-Token aktiv — Multi-Token-Mode (Rotation läuft).")

# Set für O(1) Token-Lookup
_VALID_TOKENS = {t for t in (MCP_TOKEN, MCP_TOKEN_LEGACY) if t}

# ---------- Origin/Host Whitelist (MCP-Spec MUST) ----------------------------
# Spec 2025-06-18 Transports §Security Warning: Server MUST validate Origin
# header. FastMCP TransportSecuritySettings macht das nativ wenn
# enable_dns_rebinding_protection=True UND allowed_hosts/allowed_origins
# gesetzt sind.
#
# Default-Whitelist deckt unsere Production-URLs ab. Ueberschreibbar via ENV
# (comma-separated) fuer Dev/Staging-Setups.

_DEFAULT_ALLOWED_HOSTS = [
    "wiki-mcp.sima.business",
    "76-13-10-79.sslip.io",
    "localhost",
    "127.0.0.1",
    f"localhost:{MCP_PORT}",
    f"127.0.0.1:{MCP_PORT}",
]
_DEFAULT_ALLOWED_ORIGINS = [
    "https://wiki-mcp.sima.business",
    "https://wiki-dashboard.sima.business",
    "https://76-13-10-79.sslip.io",
    "https://claude.ai",
    "https://claude.com",
]

_env_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
_env_origins = os.environ.get("MCP_ALLOWED_ORIGINS", "").strip()
ALLOWED_HOSTS = (
    [h.strip() for h in _env_hosts.split(",") if h.strip()]
    if _env_hosts else _DEFAULT_ALLOWED_HOSTS
)
ALLOWED_ORIGINS = (
    [o.strip() for o in _env_origins.split(",") if o.strip()]
    if _env_origins else _DEFAULT_ALLOWED_ORIGINS
)

log.info("Origin-Whitelist: %d Hosts + %d Origins", len(ALLOWED_HOSTS), len(ALLOWED_ORIGINS))

# streamable_http_path="/" damit der Mount unter /mcp die Tools direkt
# unter /mcp serviert (nicht unter /mcp/mcp).
#
# DNS-Rebinding-Protection AN — Spec 2025-06-18 fordert Origin-Validation
# als MUST. Bearer-Auth allein reicht nicht: ein boesartiger Browser-Tab
# koennte (theoretisch) einen XSS-geleakten Token verwenden. Mit Whitelist
# wird der Browser zum Mit-Validator: Browser blockt Cross-Origin requests
# ausserhalb der Whitelist via CORS.
mcp = FastMCP(
    "ki-os-vault",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=ALLOWED_ORIGINS,
    ),
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
        raise ToolError(str(e))


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
        raise ToolError(str(e))


@mcp.tool()
def list_files(path: str = "") -> dict[str, Any]:
    """Listet Inhalt eines Vault-Folders (nicht rekursiv).

    HINWEIS: Für Tasks lieber `list_tasks` verwenden (filtert by status
    und sortiert nach Priorität). list_files zeigt ALLE Files inkl. done.

    Args:
        path: Rel-Pfad zum Folder, "" für Vault-Root

    Returns:
        {path, entries: [{kind: 'dir'|'file', name, path, size?, mtime?}, ...]}
    """
    try:
        return {"path": path, "entries": vault.list_dir(path)}
    except VaultError as e:
        raise ToolError(str(e))


@mcp.tool()
def list_tasks(
    status: str = "open",
    project: str | None = None,
    priority: str | None = None,
    context: str | None = None,
    overdue_only: bool = False,
    due_until: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Listet Tasks aus 10_Life/tasks/ mit Filter + sinnvollem Sort.

    DEFAULT: nur offene Tasks (status='open'), sortiert nach Priorität
    (urgent → high → medium → low) und dann nach Due-Datum.

    Filter sind kombinierbar (alle als AND).

    Args:
        status: 'open' | 'done' | 'snoozed' | 'all' (default 'open')
        project: Nur Tasks mit diesem Projekt-Slug (z.B. "dachboden-ausbau")
        priority: Nur Tasks mit dieser exakten Priority
        context: Nur Tasks mit diesem Context (z.B. "@home", "@work")
        overdue_only: Nur Tasks mit due < heute (status egal)
        due_until: Nur Tasks mit due <= diesem ISO-Datum
        limit: Max Anzahl Treffer (default 30)

    Returns:
        {tasks: [{id, title, status, priority, due, project, context, path}],
         total_matched, total_in_vault}
    """
    try:
        if status not in ("open", "done", "snoozed", "all"):
            raise ToolError(f"Ungültiger status: {status} (open|done|snoozed|all)")
        tasks_dir = vault.VAULT_PATH / "10_Life" / "tasks"
        if not tasks_dir.is_dir():
            return {"tasks": [], "total_matched": 0, "total_in_vault": 0}

        today = vault.today_iso()
        all_count = 0
        matched: list[dict[str, Any]] = []

        # Context-Filter normalisieren: Vault-Convention ist drift (mal "@home",
        # mal "home"). Wir strippen das @ beidseitig damit beide Schreibweisen
        # matchen.
        ctx_norm = context.lstrip("@").lower() if context else None

        for path in tasks_dir.glob("*.md"):
            all_count += 1
            try:
                post = vault.read_post(vault.rel_path(path))
            except Exception:  # noqa: BLE001
                continue
            fm = post.metadata
            t_status = fm.get("status")
            t_due = fm.get("due")
            t_priority = fm.get("priority")
            t_project = fm.get("project")
            t_context = fm.get("context")

            # Filter
            if status != "all" and t_status != status:
                continue
            if project and t_project != project:
                continue
            if priority and t_priority != priority:
                continue
            if ctx_norm:
                t_ctx_norm = (t_context or "").lstrip("@").lower()
                if t_ctx_norm != ctx_norm:
                    continue
            if due_until and (not t_due or str(t_due) > due_until):
                continue
            if overdue_only:
                if not t_due or str(t_due) >= today:
                    continue

            matched.append(
                {
                    "id": fm.get("id"),
                    "title": fm.get("title"),
                    "status": t_status,
                    "priority": t_priority,
                    "due": str(t_due) if t_due else None,
                    "project": t_project,
                    "context": t_context,
                    "recurrence": fm.get("recurrence"),
                    "path": vault.rel_path(path),
                }
            )

        # Sort: overdue first, dann priority, dann due
        prio_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

        def sort_key(t: dict[str, Any]) -> tuple[Any, ...]:
            due = t.get("due") or "9999-12-31"
            is_overdue = bool(t.get("due")) and t["due"] < today and t["status"] == "open"
            return (
                0 if is_overdue else 1,                     # overdue zuerst
                prio_order.get(t.get("priority"), 99),      # urgent zuerst
                due,                                         # früheres due zuerst
            )

        matched.sort(key=sort_key)
        return {
            "tasks": matched[:limit],
            "total_matched": len(matched),
            "total_in_vault": all_count,
        }
    except VaultError as e:
        raise ToolError(str(e))


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
        # STRICT VALIDATOR
        if err := validators.validate_create_note(title, project, body, tags, subpath):
            raise ToolError(err)
        slug = vault.slugify(title)
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
                raise ToolError(f"Datei existiert bereits: {rel}")
        except VaultError as e:
            raise ToolError(str(e))

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

        # Auto-Link in Projekt-README updaten
        autolinked = False
        if project:
            try:
                readme = vault.VAULT_PATH / "05_Projects" / project / "README.md"
                if readme.is_file():
                    sections = vault.collect_project_content(project)
                    autolinked = vault.update_auto_notes_block(readme, sections)
            except Exception:  # noqa: BLE001
                pass

        return {"path": rel, "id": slug, "created": date, "autolinked": autolinked}
    except VaultError as e:
        raise ToolError(str(e))


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
        # STRICT VALIDATOR
        if err := validators.validate_create_task(title, priority, due, context, project, recurrence):
            raise ToolError(err)
        slug = vault.slugify(title)
        # Context auto-normalisieren (kein @-Präfix)
        if context:
            context = context.lstrip("@").lower()
        task_id = f"t-{slug}"
        rel = f"10_Life/tasks/{slug}.md"

        try:
            if vault.safe_path(rel).exists():
                raise ToolError(f"Task existiert bereits: {rel}")
        except VaultError as e:
            raise ToolError(str(e))

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
        raise ToolError(str(e))


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
    if err := validators.validate_append_to_daily(text, section, date):
        raise ToolError(err)
    try:
        rel = vault.append_to_daily(text, section=section, d=date)
        return {"path": rel, "section": section}
    except VaultError as e:
        raise ToolError(str(e))


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
    if err := validators.validate_edit_file(path, frontmatter_updates, body):
        raise ToolError(err)
    try:
        # Snapshot vorher (nur wenn body geändert wird oder destruktive FM-Ops)
        before_bytes = vault.safe_path(path).read_bytes()
        snapshot.snapshot_path(path, before_bytes, "edit")

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
        raise ToolError(str(e))


# ---------- edit_file_replace (find/replace) ---------------------------------
# Sicherheits-Konstanten 1:1 aus Bot uebernommen — wir wollen identisches
# Verhalten weil dieser Pfad bisher im Bot lebte und produktionsbewaehrt war.

EDIT_FILE_MAX_BYTES = 5 * 1024 * 1024     # 5 MB — keine Massenfile-Edits
EDIT_FILE_MAX_REGEX_LEN = 500             # >500 chars Regex = vermutlich Halluzination
# Pathological-Regex-Patterns die ReDoS triggern koennen (nested quantifiers)
_REDOS_PATTERNS = [
    re.compile(r"\([^)]*[+*]\)[+*]"),     # (a+)+ / (a*)*
    re.compile(r"\([^)]*\|[^)]*\)[+*]"),  # (a|a)+ / (a|b)*
]


@mcp.tool()
def edit_file_replace(
    path: str,
    find: str,
    replace: str,
    regex: bool = False,
) -> dict[str, Any]:
    """Find/Replace innerhalb eines Files (literal oder regex).

    Im Gegensatz zu `edit_file` (das ganze Body/FM-Felder ersetzt) macht
    dieses Tool partielle In-File-Edits — typisch fuer LLM-Korrekturen
    ("ersetze 'Donnerstag' durch 'Freitag'").

    Sicherheit:
      - Path via vault.safe_path → kein Path-Traversal
      - File-Size-Cap 5 MB → kein OOM bei Riesen-Files
      - Bei regex=True: Pattern-Length-Cap 500 chars + ReDoS-Heuristik
        (nested quantifier wie (a+)+, (a|b)* werden abgelehnt)
      - Snapshot vor Write fuer Rollback

    Args:
        path: Rel-Pfad zur Datei
        find: Such-String (literal) oder Regex-Pattern (wenn regex=True)
        replace: Ersetzungs-String. Bei regex: \\1, \\2 etc. fuer Capture-Groups.
        regex: True = find als Python-regex. False = literal substring.

    Returns:
        {path, replacements: int, file_size_bytes: int}
    """
    if err := validators.validate_edit_file_replace(path, find, replace, regex):
        raise ToolError(err)
    try:
        p = vault.safe_path(path)
    except VaultError as e:
        raise ToolError(str(e))
    if not p.is_file():
        raise ToolError(f"Datei nicht gefunden: {path}")
    size = p.stat().st_size
    if size > EDIT_FILE_MAX_BYTES:
        raise ToolError(
            f"Datei zu gross fuer edit_file_replace ({size} > {EDIT_FILE_MAX_BYTES}B)"
        )

    if regex:
        if len(find) > EDIT_FILE_MAX_REGEX_LEN:
            raise ToolError(
                f"Regex zu lang ({len(find)} > {EDIT_FILE_MAX_REGEX_LEN})"
            )
        for redos_pat in _REDOS_PATTERNS:
            if redos_pat.search(find):
                raise ToolError(
                    f"Regex-Pattern enthaelt pathologisches Konstrukt "
                    f"(nested quantifier) — ReDoS-Risiko: {find[:60]!r}"
                )
        try:
            content = p.read_text(encoding="utf-8")
            new_text, n = re.subn(find, replace, content)
        except re.error as e:
            raise ToolError(f"Regex-Syntax-Fehler in {find[:50]!r}: {e}")
    else:
        content = p.read_text(encoding="utf-8")
        n = content.count(find)
        new_text = content.replace(find, replace)

    if n == 0:
        return {"path": path, "replacements": 0,
                "file_size_bytes": size, "changed": False}

    # Snapshot vor Write
    snapshot.snapshot_path(path, content.encode("utf-8"), "edit_replace")
    p.write_text(new_text, encoding="utf-8")
    return {
        "path": path,
        "replacements": n,
        "file_size_bytes": p.stat().st_size,
        "changed": True,
    }


# ---------- append_table_row -------------------------------------------------
# Format-erhaltendes Append in eine bestehende Markdown-Tabelle.
# Verhindert die LLM-Failure-Mode "Bot prepended Prosa-Block statt Table-Row".

_TABLE_SEP_CELL = re.compile(r"^:?-+:?$")


def _is_table_separator(line: str) -> bool:
    """True wenn Zeile ein GFM-Tabellen-Separator ist: `|---|---|` oder `|:--|--:|`."""
    s = line.strip()
    if not s.startswith("|") or not s.endswith("|"):
        return False
    cells = [c.strip() for c in s[1:-1].split("|")]
    if not cells:
        return False
    return all(_TABLE_SEP_CELL.fullmatch(c) for c in cells)


def _is_table_row(line: str) -> bool:
    """True wenn Zeile aussieht wie Tabellen-Zeile (Pipe am Anfang+Ende, kein Separator)."""
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and not _is_table_separator(line)


def _heading_level(line: str) -> int:
    """Returnt 1-6 fuer #..######, sonst 0."""
    s = line.lstrip()
    if not s.startswith("#"):
        return 0
    n = 0
    for ch in s:
        if ch == "#":
            n += 1
        else:
            break
    if n > 6 or n == 0:
        return 0
    if len(s) > n and s[n] != " ":
        return 0  # `#word` ist kein Heading
    return n


def _heading_text(line: str) -> str:
    """Extrahiert Heading-Text ohne #-Praefix."""
    return line.lstrip().lstrip("#").strip()


def _find_table(lines: list[str], heading: str | None) -> tuple[int, int, int]:
    """Findet eine Tabelle im body.

    Returns:
        (header_idx, sep_idx, n_cols) — Indizes in `lines`, Spaltenzahl.

    Raises:
        ValueError mit klarer Message wenn keine/mehrere Tabellen.
    """
    # 1) Search-Range bestimmen
    if heading:
        target = heading.strip().lower()
        h_idx = -1
        h_level = 0
        for i, line in enumerate(lines):
            lvl = _heading_level(line)
            if lvl > 0 and target in _heading_text(line).lower():
                h_idx = i
                h_level = lvl
                break
        if h_idx < 0:
            raise ValueError(f"Heading nicht gefunden: {heading!r}")
        # Range: nach Heading bis zur naechsten Heading mit <= level
        start = h_idx + 1
        end = len(lines)
        for j in range(start, len(lines)):
            lvl = _heading_level(lines[j])
            if 0 < lvl <= h_level:
                end = j
                break
    else:
        start, end = 0, len(lines)

    # 2) Tabellen finden: Header-Zeile + direkt folgende Separator-Zeile
    tables: list[tuple[int, int]] = []
    i = start
    while i < end - 1:
        if _is_table_row(lines[i]) and _is_table_separator(lines[i + 1]):
            tables.append((i, i + 1))
            # skip past data rows
            j = i + 2
            while j < end and _is_table_row(lines[j]):
                j += 1
            i = j
        else:
            i += 1

    if not tables:
        scope = f"unter Heading {heading!r}" if heading else "im File"
        raise ValueError(f"Keine Markdown-Tabelle gefunden {scope}")
    if len(tables) > 1 and not heading:
        raise ValueError(
            f"Mehrere Tabellen gefunden ({len(tables)}). "
            f"Bitte `heading` setzen zum Disambiguieren."
        )

    header_idx, sep_idx = tables[0]
    # Spaltenzahl aus Header
    header_cells = [c for c in lines[header_idx].strip()[1:-1].split("|")]
    return header_idx, sep_idx, len(header_cells)


def _escape_cell(value: str) -> str:
    """Escapen fuer Markdown-Tabellen-Zelle."""
    return (
        value.replace("\r\n", "\n")
        .replace("\r", "")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", "<br>")
        .strip()
    )


@mcp.tool()
def append_table_row(
    path: str,
    values: list[str],
    heading: str | None = None,
) -> dict[str, Any]:
    """Fuegt eine Zeile an eine bestehende Markdown-Tabelle.

    Bevorzugte Methode fuer Listen-/Stunden-/Inventar-Files: die Tabelle
    wird automatisch erkannt (Pipe-Header + Separator-Zeile), Zeile wird
    Format-erhaltend ans Ende der Tabelle angefuegt. Kein Prosa-Block,
    kein Markdown-Drift.

    Tabellen-Erkennung:
      | A | B | C |   <- Header
      |---|---|---|   <- Separator
      | x | y | z |   <- Daten

    Disambiguierung bei mehreren Tabellen: `heading` setzen (substring
    eines H1/H2/H3 — Tabelle wird unter dieser Heading gesucht).

    Trailing-empty-row als Spacer wird respektiert: wenn die letzte
    Zeile ein leerer Spacer (`| | | |`) ist, wird DAVOR eingefuegt.

    Args:
        path: Rel-Pfad zur .md-Datei
        values: Cell-Werte als list[str]. Anzahl MUSS exakt zu Tabellen-
                Spalten passen (Schema-Strict). Pipes/Newlines werden
                automatisch escaped.
        heading: Optional. H1/H2/H3-Substring um Tabelle zu adressieren
                 wenn mehrere im File sind.

    Returns:
        {path, columns, total_data_rows_after, snapshot}
    """
    if err := validators.validate_append_table_row(path, values, heading):
        raise ToolError(err)

    try:
        post = vault.read_post(path)
    except VaultError as e:
        raise ToolError(str(e))

    body = post.content
    lines = body.split("\n")

    try:
        header_idx, sep_idx, n_cols = _find_table(lines, heading)
    except ValueError as e:
        raise ToolError(str(e))

    if len(values) != n_cols:
        raise ToolError(
            f"Spaltenzahl-Mismatch: Tabelle hat {n_cols} Spalten, "
            f"values hat {len(values)}. Tabellen-Header: {lines[header_idx]!r}"
        )

    # Letzte Daten-Zeile in der Tabelle finden
    last_data_idx = sep_idx
    for j in range(sep_idx + 1, len(lines)):
        if _is_table_row(lines[j]):
            last_data_idx = j
        else:
            break

    # Insert-Position: vor Spacer-Row (alle Cells leer) sonst danach
    def _is_spacer_row(line: str) -> bool:
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            return False
        cells = [c.strip() for c in s[1:-1].split("|")]
        return all(c == "" for c in cells)

    if last_data_idx > sep_idx and _is_spacer_row(lines[last_data_idx]):
        insert_at = last_data_idx
    else:
        insert_at = last_data_idx + 1

    new_row = "| " + " | ".join(_escape_cell(v) for v in values) + " |"

    # Snapshot vor Write
    before_bytes = vault.safe_path(path).read_bytes()
    snapshot.snapshot_path(path, before_bytes, "append_table_row")

    # Insert + write
    lines.insert(insert_at, new_row)
    post.content = "\n".join(lines)
    vault.write_post(path, post)

    # Anzahl Daten-Zeilen nach Insert (alle Tabellen-Rows nach Separator,
    # ohne Spacer)
    data_rows = 0
    for j in range(sep_idx + 1, len(lines)):
        if not _is_table_row(lines[j]):
            break
        if not _is_spacer_row(lines[j]):
            data_rows += 1

    return {
        "path": path,
        "columns": n_cols,
        "total_data_rows_after": data_rows,
        "inserted_at_line": insert_at,
    }


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
        # STRICT VALIDATOR
        if err := validators.validate_task_action(id, action, snooze_until, due, priority):
            raise ToolError(err)

        task_path = vault.find_task(id)
        if not task_path:
            raise ToolError(f"Task nicht gefunden: {id}")
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
                raise ToolError("snooze braucht snooze_until (oder due) Parameter")
            post["status"] = "snoozed"
            post["due"] = target
        elif action == "edit":
            if priority is not None:
                if priority not in ("urgent", "high", "medium", "low"):
                    raise ToolError(f"Ungültige Priorität: {priority}")
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
        raise ToolError(str(e))


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
        # STRICT VALIDATOR
        if err := validators.validate_create_meeting(title, attendees, project, date):
            raise ToolError(err)
        d = date or vault.today_iso()
        slug = vault.slugify(title)
        filename = f"{d}_{slug}.md"
        if project:
            rel = f"05_Projects/{project}/meetings/{filename}"
        else:
            rel = f"10_Life/meetings/{filename}"

        try:
            if vault.safe_path(rel).exists():
                raise ToolError(f"Meeting existiert bereits: {rel}")
        except VaultError as e:
            raise ToolError(str(e))

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

        # Auto-Link in Projekt-README updaten
        autolinked = False
        if project:
            try:
                readme = vault.VAULT_PATH / "05_Projects" / project / "README.md"
                if readme.is_file():
                    sections = vault.collect_project_content(project)
                    autolinked = vault.update_auto_notes_block(readme, sections)
            except Exception:  # noqa: BLE001
                pass

        return {"path": rel, "id": slug, "autolinked": autolinked}
    except VaultError as e:
        raise ToolError(str(e))


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
            raise ToolError(f"Datei nicht gefunden: {path}")
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
        raise ToolError(str(e))


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
        raise ToolError(f"Token unbekannt oder abgelaufen: {token}")
    try:
        # Snapshot vor Delete
        path = pending["path"]
        full = vault.safe_path(path)
        if full.is_file():
            snapshot.snapshot_path(path, full.read_bytes(), "delete")
        vault.delete_file(path)
        return {
            "deleted": True,
            "path": path,
            "requested_at": pending["requested_at"],
            "reason": pending.get("reason", ""),
        }
    except VaultError as e:
        # Bei Fehler Token wieder einsetzen damit Retry möglich ist
        _pending_deletes[token] = pending
        raise ToolError(str(e))


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
    if err := validators.validate_move(source, dest):
        raise ToolError(err)
    try:
        src = vault.safe_path(source)
        if not src.is_file():
            raise ToolError(f"Quelldatei nicht gefunden: {source}")
        dst = vault.safe_path(dest)
        if dst.exists():
            raise ToolError(f"Zieldatei existiert bereits: {dest}")

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

        # Wikilink-Refs sammeln + Related-Refs (FM-Felder mit old_id in `related[]`)
        refs: list[tuple[Any, list[int]]] = []
        related_only_paths: list[Any] = []  # Files mit related[] aber ohne Body-WL
        if old_id and new_id and old_id != new_id and update_links:
            refs = vault.find_wikilink_refs(old_id)
            wl_paths = {p for p, _ in refs}
            for p in vault.find_related_refs(old_id):
                if p not in wl_paths:
                    related_only_paths.append(p)

        refs_summary = [
            {"path": vault.rel_path(p), "lines": lines, "kind": "wikilink"}
            for p, lines in refs
        ] + [
            {"path": vault.rel_path(p), "lines": [], "kind": "related-fm-only"}
            for p in related_only_paths
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

        # Snapshot vorher: source + alle to-be-updated Files
        snapshot_files: dict[str, bytes] = {}
        try:
            snapshot_files[source] = src.read_bytes()
        except OSError:
            pass
        for p in (
            [pp for pp, _ in refs] + related_only_paths
            if (old_id and new_id and old_id != new_id and update_links)
            else []
        ):
            if p == dst:
                continue
            try:
                snapshot_files[vault.rel_path(p)] = p.read_bytes()
            except OSError:
                pass
        snapshot.snapshot("move", snapshot_files)

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

        # Wikilink-Updates + FM related[] Updates in anderen Files
        updated_files: list[dict[str, Any]] = []
        if old_id and new_id and old_id != new_id and update_links:
            # Vereinige: Files mit Body-Wikilinks + Files mit nur related[] FM
            all_paths_to_check = [p for p, _ in refs] + related_only_paths
            for path in all_paths_to_check:
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
        raise ToolError(str(e))


# ---------- move_bulk: mehrere Files in einen Ordner ------------------------


@mcp.tool()
def move_bulk(
    sources: list[str],
    dest_dir: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Verschiebt mehrere Dateien/Ordner in einen Ziel-Ordner.

    Spart pro Item einen Tool-Call (LLM-Loop-Iterationen). Pro Item wird
    Erfolg/Fehler getrennt gemeldet, alle teilen sich denselben Tool-Call.

    Wikilinks werden NICHT migriert (anders als beim single-file `move`):
    bei Bulk-Move bleiben die IDs typischerweise gleich (gleicher Filename),
    nur der Ordner aendert sich. Falls eine Bulk-Op auch IDs aendert (z.B.
    Datei rename plus move), den Single-`move`-Pfad pro Datei nutzen.

    Args:
        sources: Liste von vault-relativen Pfaden (Files oder Ordner)
        dest_dir: vault-relativer Zielordner (wird angelegt wenn fehlt)
        overwrite: True erlaubt Ueberschreiben existierender Files

    Returns:
        {moved: [name, ...], failed: [{name, reason}, ...],
         dest_dir: str, success_count: int, fail_count: int}
    """
    if err := validators.validate_move_bulk(sources, dest_dir, overwrite):
        raise ToolError(err)
    try:
        dst = vault.safe_path(dest_dir)
    except VaultError as e:
        raise ToolError(f"dest_dir: {e}")

    if dst.exists() and not dst.is_dir():
        raise ToolError(f"Ziel ist eine Datei, kein Ordner: {dest_dir}")
    dst.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    failed: list[dict[str, str]] = []

    for src_rel in sources:
        if not isinstance(src_rel, str) or not src_rel.strip():
            failed.append({"name": str(src_rel), "reason": "leerer/ungueltiger Eintrag"})
            continue
        try:
            src = vault.safe_path(src_rel)
        except VaultError as e:
            failed.append({"name": src_rel, "reason": f"Pfad: {e}"})
            continue
        if not src.exists():
            failed.append({"name": src_rel, "reason": "nicht gefunden"})
            continue
        if src == vault.VAULT_PATH:
            failed.append({"name": src_rel, "reason": "Vault-Root unbeweglich"})
            continue

        final = dst / src.name
        if final.exists():
            if not overwrite:
                failed.append({"name": src.name, "reason": "Ziel existiert (overwrite=false)"})
                continue
            try:
                if final.is_dir():
                    shutil.rmtree(final)
                else:
                    final.unlink()
            except Exception as e:  # noqa: BLE001
                failed.append({"name": src.name, "reason": f"overwrite-cleanup: {e}"})
                continue

        try:
            shutil.move(str(src), str(final))
            moved.append(src.name)
        except Exception as e:  # noqa: BLE001
            failed.append({"name": src.name, "reason": str(e)})

    return {
        "moved": moved,
        "failed": failed,
        "dest_dir": dest_dir,
        "success_count": len(moved),
        "fail_count": len(failed),
    }


# ---------- move_project: Projekt-Folder verschieben/nesten ------------------


def _find_project_dir(slug: str) -> Any:
    """Sucht 05_Projects/<slug>/ rekursiv (Subprojekte erlaubt). Returns Path or None."""
    projects_root = vault.VAULT_PATH / "05_Projects"
    if not projects_root.is_dir():
        return None
    slug = slug.strip().lower()
    if slug.startswith("project-"):
        slug = slug[len("project-"):]
    # 1. Top-level
    top = projects_root / slug
    if top.is_dir():
        return top
    # 2. Rekursiv (Subprojekte)
    matches = []
    for d in projects_root.rglob("*"):
        if d.is_dir() and d.name == slug:
            matches.append(d)
    if len(matches) == 1:
        return matches[0]
    return None  # nicht gefunden ODER mehrdeutig


@mcp.tool()
def move_project(
    slug: str,
    parent: str | None = None,
) -> dict[str, Any]:
    """Verschiebt ein Projekt unter `parent` (= macht es zum Subprojekt) ODER
    zurueck auf Top-Level (parent=None).

    Slug + parent werden case-insensitive aufgeloest (auch mit `project-` Prefix).
    Subprojekt-Suche ist rekursiv → findet Projekte ueberall unter 05_Projects/.
    Schleifen-Schutz: ein Projekt kann nicht in seinen eigenen Sub-Tree verschoben werden.

    Wikilinks werden NICHT geupdated (anders als beim single-file `move`):
    Projekte werden via [[id]] referenziert wo `id = "project-<slug>"`. Slug
    bleibt beim Move gleich, nur der Pfad aendert sich → keine Link-Updates noetig.
    Frontmatter `project: <slug>` der enthaltenen Notes/Meetings bleibt korrekt.

    Args:
        slug: Projekt-Slug (z.B. "matura" oder "project-matura")
        parent: neuer Parent-Slug (None = Top-Level)

    Returns:
        {old_path, new_path, parent, status}
    """
    if err := validators.validate_move_project(slug, parent):
        raise ToolError(err)
    src = _find_project_dir(slug)
    if src is None:
        raise ToolError(f"Projekt nicht gefunden (oder mehrdeutig): {slug}")

    projects_root = vault.VAULT_PATH / "05_Projects"

    # Ziel bestimmen
    if parent and parent.strip():
        clean_parent = parent.strip().lower()
        if clean_parent.startswith("project-"):
            clean_parent = clean_parent[len("project-"):]
        clean_slug = src.name
        if clean_parent == clean_slug:
            raise ToolError("Projekt kann nicht sich selbst als Parent haben")
        parent_dir = _find_project_dir(clean_parent)
        if parent_dir is None:
            raise ToolError(f"Parent-Projekt nicht gefunden: {parent}")
        # Zyklen-Schutz: parent darf nicht unter src liegen
        try:
            parent_dir.relative_to(src)
            raise ToolError(
                f"Parent {parent!r} liegt bereits unter {clean_slug!r} — wuerde Schleife erzeugen"
            )
        except ValueError:
            pass
        dst = parent_dir / clean_slug
    else:
        dst = projects_root / src.name

    if dst.resolve() == src.resolve():
        return {
            "old_path": vault.rel_path(src),
            "new_path": vault.rel_path(src),
            "parent": parent,
            "status": "no_change",
        }
    if dst.exists():
        raise ToolError(f"Ziel existiert bereits: {vault.rel_path(dst)}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"move fehlgeschlagen: {e}")

    return {
        "old_path": str(src.relative_to(vault.VAULT_PATH)).replace("\\", "/"),
        "new_path": vault.rel_path(dst),
        "parent": parent,
        "status": "moved",
    }


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
    if err := validators.validate_goal_log(goal, text, subtype):
        raise ToolError(err)
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
        raise ToolError(str(e))


# ---------- create_project: Projekt-Container anlegen ------------------------


@mcp.tool()
def create_project(
    name: str,
    description: str | None = None,
    parent: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Legt einen neuen Projekt-Container unter 05_Projects/<slug>/ an.

    Erstellt:
      - Ordner 05_Projects/<slug>/ (oder unter <parent>/<slug>/ als Subprojekt)
      - README.md mit Frontmatter (id=project-<slug>, type=project, status=active)
        plus Dataview-Queries fuer offene Tasks/Notes/Meetings die `project: <slug>`
        in der Frontmatter haben.
      - CONTEXT.md leeres Skeleton — kann via project_context() befuellt werden.

    Args:
        name: Projekt-Name (Anzeige-Titel, wird zu slug konvertiert)
        description: Optional Beschreibungstext fuer README-Kopf (default Standard-Phrase)
        parent: Optional Slug eines existierenden Projekts → Subprojekt
        tags: Optional Tag-Liste fuer Frontmatter

    Idempotent gegen Doppel-Anlagen: wenn Slug schon irgendwo unter
    05_Projects/ existiert (rekursiv), wird abgebrochen.

    Returns:
        {slug, path, id, parent?, status='created'}
    """
    if err := validators.validate_create_project(name, parent):
        raise ToolError(err)

    slug = vault.slugify(name)
    if not slug or slug == "untitled":
        raise ToolError(f"Slug aus {name!r} nicht ableitbar — aussagekraeftigeren Namen waehlen")

    # Existenz-Check rekursiv (verhindert Doppel-Slug an verschiedenen Orten)
    existing = _find_project_dir(slug)
    if existing is not None:
        rel = vault.rel_path(existing)
        return {
            "slug": slug,
            "path": rel,
            "id": f"project-{slug}",
            "status": "exists",
            "reason": f"Projekt existiert bereits: {rel}/",
        }

    # Parent aufloesen falls Subprojekt
    projects_root = vault.VAULT_PATH / "05_Projects"
    projects_root.mkdir(parents=True, exist_ok=True)

    if parent:
        parent_dir = _find_project_dir(parent)
        if parent_dir is None:
            raise ToolError(f"Parent-Projekt {parent!r} nicht gefunden (oder mehrdeutig)")
        proj_dir = parent_dir / slug
    else:
        proj_dir = projects_root / slug

    proj_dir.mkdir(parents=True, exist_ok=False)

    today = vault.today_iso()
    desc = description.strip() if description and description.strip() else (
        f"Projekt-Container fuer **{name}**. Tasks/Notes/Meetings mit "
        f"`project: {slug}` im Frontmatter werden hier automatisch gelistet."
    )

    # README mit Dataview-Queries — gleiche Struktur wie Bot's alte create_project
    body = (
        f"# {name}\n\n"
        f"{desc}\n\n"
        f"## Status\n"
        f"- **Status**: active\n"
        f"- **Gestartet**: {today}\n\n"
        f"## Offene Tasks\n"
        f"```dataview\n"
        f"TABLE WITHOUT ID file.link AS Task, priority AS Prio, due AS Faellig\n"
        f'FROM "10_Life/tasks"\n'
        f'WHERE project = "{slug}" AND status != "done" AND status != "cancelled"\n'
        f"SORT priority DESC, due ASC\n"
        f"```\n\n"
        f"## Notizen\n"
        f"```dataview\n"
        f"LIST\n"
        f'FROM "10_Life/notes"\n'
        f'WHERE project = "{slug}"\n'
        f"SORT file.ctime DESC\n"
        f"```\n\n"
        f"## Meetings\n"
        f"```dataview\n"
        f"TABLE WITHOUT ID file.link AS Meeting, date AS Datum\n"
        f'FROM "10_Life/meetings"\n'
        f'WHERE project = "{slug}"\n'
        f"SORT date DESC\n"
        f"```\n\n"
        f"## Log\n"
        f"- {today}: Projekt angelegt\n"
    )

    fm = {
        "id": f"project-{slug}",
        "title": name,
        "type": "project",
        "started": today,
        "status": "active",
        "tags": list(tags or []),
    }
    post = frontmatter.Post(body, **fm)
    readme_path = proj_dir / "README.md"
    readme_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

    # CONTEXT.md leeres Skeleton — befuellt via project_context() spaeter
    context_path = proj_dir / "CONTEXT.md"
    context_path.write_text(
        f"# Kontext: {slug}\n\n"
        "_Projekt-spezifische Regeln/Infos (Auftraggeber, Tech-Stack, Frist, "
        f"Budget). Wird automatisch in den Bot-Prompt geladen wenn `activate_project({slug})` aktiv._\n\n"
        "_(noch leer — fuelle via Bot-Tool `project_context(action='update')` "
        "oder direkt im Editor)_\n",
        encoding="utf-8",
    )

    return {
        "slug": slug,
        "path": vault.rel_path(proj_dir),
        "id": f"project-{slug}",
        "parent": parent,
        "status": "created",
    }


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
    if err := validators.validate_project_context(project, text, mode):
        raise ToolError(err)
    try:
        if mode not in ("append", "replace"):
            raise ToolError(f"mode muss 'append' oder 'replace' sein, nicht {mode}")
        rel = f"05_Projects/{project}/CONTEXT.md"
        p = vault.safe_path(rel)
        if not p.parent.is_dir():
            raise ToolError(f"Projekt-Folder nicht gefunden: 05_Projects/{project}/")
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
        raise ToolError(str(e))


@mcp.tool()
def read_project_context(project: str) -> dict[str, Any]:
    """Liest CONTEXT.md eines Projekts.

    Path: 05_Projects/<project>/CONTEXT.md (rekursive Suche im Tree).

    Args:
        project: Projekt-Slug (mit oder ohne 'project-' Prefix)

    Returns:
        {project, path, exists, content} — content ist leer wenn Datei fehlt.
    """
    if err := validators.validate_read_project_context(project):
        raise ToolError(err)
    proj_dir = _find_project_dir(project)
    if proj_dir is None:
        return {
            "project": project,
            "path": None,
            "exists": False,
            "content": "",
            "reason": "Projekt-Folder nicht gefunden",
        }

    context_file = proj_dir / "CONTEXT.md"
    if not context_file.is_file():
        return {
            "project": proj_dir.name,
            "path": vault.rel_path(context_file),
            "exists": False,
            "content": "",
        }

    try:
        content = context_file.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as e:
        raise ToolError(f"Lese-Fehler {context_file.name}: {e}")

    return {
        "project": proj_dir.name,
        "path": vault.rel_path(context_file),
        "exists": True,
        "content": content,
    }


# ---------- Block 3: Premium-Tools -------------------------------------------


@mcp.tool()
def daily_briefing(date: str | None = None) -> dict[str, Any]:
    """Tagesbriefing — was steht an, was ist überfällig, was lief gut.

    Liefert ein strukturiertes Dictionary mit:
      - overdue: Tasks mit due < heute (open, sortiert nach due)
      - today: Tasks mit due == heute (open, prio-sortiert)
      - upcoming_3d: Tasks mit due in 1-3 Tagen
      - inbox: Tasks ohne Projekt (open, prio-sortiert, max 10)
      - recently_done: Tasks die gestern oder heute erledigt wurden
      - daily_path: Pfad der heutigen Daily-Note
      - daily_exists: ob die heutige Daily schon existiert

    Args:
        date: ISO-Datum als Bezugsdatum (default heute)

    Returns:
        Dictionary mit allen Sektionen + total counts.
    """
    try:
        ref = date or vault.today_iso()
        from datetime import date as _date, timedelta
        ref_d = _date.fromisoformat(ref)
        in_3d = (ref_d + timedelta(days=3)).isoformat()
        yesterday = (ref_d - timedelta(days=1)).isoformat()

        tasks_dir = vault.VAULT_PATH / "10_Life" / "tasks"
        all_tasks: list[dict[str, Any]] = []
        if tasks_dir.is_dir():
            for path in tasks_dir.glob("*.md"):
                try:
                    post = vault.read_post(vault.rel_path(path))
                except Exception:  # noqa: BLE001
                    continue
                fm = post.metadata
                all_tasks.append({
                    "id": fm.get("id"),
                    "title": fm.get("title"),
                    "status": fm.get("status"),
                    "priority": fm.get("priority"),
                    "due": str(fm.get("due")) if fm.get("due") else None,
                    "project": fm.get("project"),
                    "context": fm.get("context"),
                    "last_completed": str(fm.get("last_completed")) if fm.get("last_completed") else None,
                    "path": vault.rel_path(path),
                })

        prio_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}

        def by_prio_due(t):
            return (prio_order.get(t.get("priority"), 99), t.get("due") or "9999")

        overdue = sorted(
            [t for t in all_tasks if t["status"] == "open" and t["due"] and t["due"] < ref],
            key=by_prio_due,
        )
        today = sorted(
            [t for t in all_tasks if t["status"] == "open" and t["due"] == ref],
            key=by_prio_due,
        )
        upcoming = sorted(
            [t for t in all_tasks if t["status"] == "open" and t["due"] and ref < t["due"] <= in_3d],
            key=by_prio_due,
        )
        inbox = sorted(
            [t for t in all_tasks if t["status"] == "open" and not t["project"] and not t["due"]],
            key=by_prio_due,
        )[:10]
        recently_done = sorted(
            [t for t in all_tasks if t["status"] == "done" and t["last_completed"] in (ref, yesterday)],
            key=lambda t: t.get("last_completed") or "",
            reverse=True,
        )

        daily_rel = f"10_Life/daily/{ref}.md"
        daily_exists = vault.safe_path(daily_rel).is_file()

        return {
            "date": ref,
            "overdue": overdue,
            "today": today,
            "upcoming_3d": upcoming,
            "inbox": inbox,
            "recently_done": recently_done,
            "daily_path": daily_rel,
            "daily_exists": daily_exists,
            "summary": {
                "overdue_count": len(overdue),
                "today_count": len(today),
                "upcoming_count": len(upcoming),
                "inbox_count": len(inbox),
                "recently_done_count": len(recently_done),
            },
        }
    except VaultError as e:
        raise ToolError(str(e))


# ============================================================================
# READ-TOOLS für 5y-Goal-Tracker (Vision, Säulen, Drift, Habits, Sport, ...)
# ============================================================================
# Alle Reader spiegeln das Format das Dashboard `lib/vault.ts` heute selbst
# berechnet, damit Bot + Dashboard ein 1:1-Replace machen können.

# Habit-Spalten in der Reihenfolge wie sie in tracker/habits.md stehen.
# Muss SYNC bleiben mit dashboard/lib/vault.ts HABIT_KEYS.
HABIT_KEYS = [
    {"key": "sport", "label": "Sport", "target": "3-5 km"},
    {"key": "lesen", "label": "Lesen", "target": "30 min"},
    {"key": "schlaf", "label": "Schlaf", "target": "7+ h"},
    {"key": "bildschirm", "label": "Bildschirm", "target": "< 22:30"},
    {"key": "vision", "label": "Vision", "target": "1× lesen"},
    {"key": "wasser", "label": "Wasser", "target": "2 L"},
]


def _read_goal_text(*parts: str) -> str | None:
    """Liest Datei unter 10_Life/goals/5y-2031/<parts>. None wenn fehlt."""
    p = vault.goal_file(*parts)
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return None


@mcp.tool()
def read_vision() -> dict[str, Any]:
    """Liest den Manifesto-Block aus 10_Life/goals/5y-2031/vision.md.

    Format: erstes Blockquote mit Bold (`> **...**`).
    Output: {text: str, found: bool}
    """
    raw = _read_goal_text("vision.md")
    if raw is None:
        return {"text": "", "found": False, "reason": "vision.md nicht gefunden"}
    import re as _re
    m = _re.search(r">\s*\*\*([\s\S]+?)\*\*", raw)
    if not m:
        return {"text": "", "found": False, "reason": "kein Manifesto-Block im File"}
    text = _re.sub(r"\s+", " ", _re.sub(r"\n>\s*", " ", m.group(1))).strip()
    return {"text": text, "found": True}


@mcp.tool()
def read_saeulen() -> dict[str, Any]:
    """Liest die Säulen-Tabelle aus 10_Life/goals/5y-2031/readme.md.

    Tabellenkopf: | Säule | Status | Nächster Anker | ...
    Output: {saeulen: [{slug, label, status, kpi, drift, note}, ...], total}
    """
    raw = _read_goal_text("readme.md")
    if raw is None:
        return {"saeulen": [], "total": 0, "reason": "readme.md nicht gefunden"}
    import re as _re
    m = _re.search(
        r"\|\s*Säule\s*\|\s*Status\s*\|\s*Nächster Anker\s*\|[^\n]*\n((?:\|[^\n]*\|\n)+)",
        raw,
    )
    if not m:
        return {"saeulen": [], "total": 0, "reason": "Säulen-Tabelle nicht gefunden"}
    out: list[dict[str, Any]] = []
    for row in m.group(1).strip().split("\n"):
        cells = [c.strip() for c in row.split("|")[1:-1]]
        if len(cells) < 2:
            continue
        if all(_re.fullmatch(r"-+", c or "") for c in cells):
            continue  # Header-Separator
        out.append({
            "slug": cells[0].lower(),
            "label": cells[0],
            "status": "info",
            "kpi": cells[1] if len(cells) > 1 else "",
            "drift": 0,
            "note": cells[2] if len(cells) > 2 else "",
        })
    return {"saeulen": out, "total": len(out)}


@mcp.tool()
def read_drift() -> dict[str, Any]:
    """Liest Drift-Anker (weekly/monthly/quarterly) aus readme.md.

    Pattern: `**Letzter Wochen-Anker:** YYYY-MM-DD` (oder `—`).
    Output: {weekly, monthly, quarterly} — fehlende Keys absent.
    """
    raw = _read_goal_text("readme.md")
    if raw is None:
        return {"reason": "readme.md nicht gefunden"}
    import re as _re
    out: dict[str, Any] = {}
    for label, key in (
        ("Letzter Wochen-Anker", "weekly"),
        ("Letzter Monats-Anker", "monthly"),
        ("Letzter Quartals-Anker", "quarterly"),
    ):
        m = _re.search(rf"\*\*{label}:\*\*\s*([\d-]+|—)", raw)
        if m:
            out[key] = m.group(1)
    return out


@mcp.tool()
def read_habits(days: int = 30) -> dict[str, Any]:
    """Liest Habit-Tabelle aus tracker/habits.md.

    Format pro Zeile: `| YYYY-MM-DD | ✓/✗/- | ✓/✗/- | ... |`
    Spalten in Reihenfolge: Sport, Lesen, Schlaf, Bildschirm, Vision, Wasser.
    Mapping: ✓=ok, ✗=bad, sonst=skip.

    Args:
        days: Anzahl Tage zurück inkl. heute (default 30, max 365)

    Returns:
        {habits: [{date, values: {sport, lesen, ...}}, ...], keys: [...], total}
    """
    days = max(1, min(int(days), 365))
    raw = _read_goal_text("tracker", "habits.md")
    if raw is None:
        return {"habits": [], "keys": HABIT_KEYS, "total": 0,
                "reason": "habits.md nicht gefunden"}
    import re as _re
    rows: dict[str, dict[str, str]] = {}
    for line in raw.split("\n"):
        m = _re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.+)\|$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        values: dict[str, str] = {}
        for i, h in enumerate(HABIT_KEYS):
            c = cells[i] if i < len(cells) else ""
            values[h["key"]] = "ok" if c == "✓" else "bad" if c == "✗" else "skip"
        rows[m.group(1)] = values

    from datetime import date as _date, timedelta
    today = _date.today()
    out: list[dict[str, Any]] = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        out.append({"date": d, "values": rows.get(d, {})})
    return {"habits": out, "keys": HABIT_KEYS, "total": len(out)}


@mcp.tool()
def read_sport(limit: int | None = None) -> dict[str, Any]:
    """Liest Sport-Sessions aus tracker/sport-log.md.

    Tabellen-Format: | YYYY-MM-DD | cardio|kraft | dauer_min | notiz |
    Sortiert nach Datum absteigend (neueste zuerst).

    Args:
        limit: max Anzahl Sessions (None = alle)

    Returns:
        {sessions: [{date, art, dauer, notiz}, ...], total}
    """
    raw = _read_goal_text("tracker", "sport-log.md")
    if raw is None:
        return {"sessions": [], "total": 0, "reason": "sport-log.md nicht gefunden"}
    import re as _re
    out: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        m = _re.match(
            r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(cardio|kraft)\s*\|\s*(\d+)\s*\|\s*(.*?)\s*\|$",
            line,
        )
        if not m:
            continue
        out.append({
            "date": m.group(1),
            "art": m.group(2),
            "dauer": int(m.group(3)),
            "notiz": m.group(4),
        })
    out.sort(key=lambda s: s["date"], reverse=True)
    if limit is not None:
        out = out[: max(0, int(limit))]
    return {"sessions": out, "total": len(out)}


@mcp.tool()
def read_books() -> dict[str, Any]:
    """Liest Bücher aus tracker/lesen.md (3 Sektionen: Aktiv/Geplant/Abgeschlossen).

    Aktiv/Abgeschlossen sind Markdown-Tabellen mit
    `| # | Titel | Autor | ... | Start | Ende | Lesson |`.
    Geplant ist eine Bullet-Liste `- *Titel* — Autor`.

    Output: {books: [{num?, title, author?, status, start?, ende?, lesson?}, ...], total}
    """
    raw = _read_goal_text("tracker", "lesen.md")
    if raw is None:
        return {"books": [], "total": 0, "reason": "lesen.md nicht gefunden"}
    import re as _re
    out: list[dict[str, Any]] = []
    sections = _re.split(r"^##\s+", raw, flags=_re.MULTILINE)
    for section in sections:
        title_m = _re.match(r"^(Aktiv|Geplant|Abgeschlossen)", section, _re.IGNORECASE)
        if not title_m:
            continue
        status = title_m.group(1).lower()
        if status == "geplant":
            for line in section.split("\n"):
                bm = _re.match(r"^-\s+\*?(.+?)\*?\s*(?:—|-)\s*(.+?)$", line)
                if bm:
                    out.append({
                        "title": bm.group(1).strip(),
                        "author": bm.group(2).strip(),
                        "status": "geplant",
                    })
            continue
        for line in section.split("\n"):
            if not line.startswith("|") or "---" in line:
                continue
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) < 2 or not cells[0] or cells[0] == "#":
                continue
            try:
                num: int | None = int(cells[0])
            except ValueError:
                num = None
            book: dict[str, Any] = {
                "title": (cells[1] if len(cells) > 1 else cells[0]) or "(?)",
                "status": status,
            }
            if num is not None:
                book["num"] = num
            if len(cells) > 2 and cells[2]:
                book["author"] = cells[2]
            if len(cells) > 4 and cells[4]:
                book["start"] = cells[4]
            if len(cells) > 5 and cells[5]:
                book["ende"] = cells[5]
            if cells[-1]:
                book["lesson"] = cells[-1]
            out.append(book)
    return {"books": out, "total": len(out)}


@mcp.tool()
def read_wins(days: int | None = None) -> dict[str, Any]:
    """Liest Wins aus tracker/wins.md.

    Format: `## YYYY-MM-DD\\n- Win-Text\\n- ...` (mehrere Datums-Sektionen).

    Args:
        days: nur letzte N Tage (None = alle)

    Returns:
        {wins: [{date, saeule|null, text}, ...], total, by_date: {YYYY-MM-DD: count}}
    """
    raw = _read_goal_text("tracker", "wins.md")
    if raw is None:
        return {"wins": [], "total": 0, "by_date": {}, "reason": "wins.md nicht gefunden"}
    import re as _re
    out: list[dict[str, Any]] = []
    current_date: str | None = None
    for line in raw.split("\n"):
        dm = _re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", line)
        if dm:
            current_date = dm.group(1)
            continue
        if not current_date:
            continue
        bm = _re.match(r"^-\s+(.+)", line)
        if bm:
            out.append({"date": current_date, "saeule": None, "text": bm.group(1).strip()})

    if days is not None:
        from datetime import date as _date, timedelta
        cutoff = (_date.today() - timedelta(days=int(days))).isoformat()
        out = [w for w in out if w["date"] >= cutoff]

    by_date: dict[str, int] = {}
    for w in out:
        by_date[w["date"]] = by_date.get(w["date"], 0) + 1
    return {"wins": out, "total": len(out), "by_date": by_date}


@mcp.tool()
def compute_streak() -> dict[str, Any]:
    """Berechnet Habit-Streak (current + best) aus tracker/habits.md.

    Definition: ein Tag zählt als "ok" wenn mind. 1 Habit `✓` ist.
    Lookback: letzte 180 Tage (best-streak innerhalb dieses Fensters).

    Output: {current: int, best: int, days_evaluated: int}
    """
    res = read_habits(180)  # type: ignore[no-untyped-call]
    habits: list[dict[str, Any]] = res.get("habits", [])
    best = 0
    run = 0
    for day in habits:
        any_ok = any(v == "ok" for v in day["values"].values())
        if any_ok:
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    current = 0
    for day in reversed(habits):
        any_ok = any(v == "ok" for v in day["values"].values())
        if any_ok:
            current += 1
        else:
            break
    return {"current": current, "best": best, "days_evaluated": len(habits)}


@mcp.tool()
def read_reminders() -> dict[str, Any]:
    """Liest 06_Meta/reminders.json (vom Bot gepflegte Reminder-Liste).

    Output: {reminders: [{id, fire_at, message, recurrence?}, ...], total}
    """
    p = vault.VAULT_PATH / "06_Meta" / "reminders.json"
    if not p.is_file():
        return {"reminders": [], "total": 0, "reason": "reminders.json nicht gefunden"}
    import json as _json
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"reminders": [], "total": 0, "reason": f"Parse-Fehler: {e}"}
    if not isinstance(data, list):
        return {"reminders": [], "total": 0, "reason": "Erwarte JSON-Array"}
    return {"reminders": data, "total": len(data)}


@mcp.tool()
def read_yesterday_daily() -> dict[str, Any]:
    """Liest Frontmatter der gestrigen Daily-Note (für Recap auf Heute-Page).

    Output: {found, date, energy?, mood?, key_insight?}
    """
    from datetime import date as _date, timedelta
    yest = (_date.today() - timedelta(days=1)).isoformat()
    rel = f"10_Life/daily/{yest}.md"
    p = vault.safe_path(rel)
    if not p.is_file():
        return {"found": False, "date": yest}
    try:
        post = vault.read_post(rel)
    except VaultError as e:
        return {"found": False, "date": yest, "reason": str(e)}
    fm = post.metadata
    out: dict[str, Any] = {"found": True, "date": yest}
    for k in ("energy", "mood", "key_insight"):
        v = fm.get(k)
        if v is not None:
            out[k] = v
    return out


@mcp.tool()
def vault_lint(include_structural: bool = False) -> dict[str, Any]:
    """Vault-Schema-Linter — findet Drift und Inkonsistenzen.

    Prüft (default smart-mode — Templates/READMEs werden gefiltert):
      - broken_wikilinks: [[id]] die auf nicht-existente IDs zeigen
      - duplicate_ids: gleiche FM-ID in mehreren Files
      - missing_fm: Files ohne YAML-Frontmatter
      - missing_required: Files mit fehlenden Pflichtfeldern (id, type, title)
      - context_drift: Tasks mit `context` ohne führendes @ (oder umgekehrt)
      - orphans: Notes ohne `[[id]]` Backlinks von anderen Files

    Default-Filter (im Smart-Mode aus dem Scan ausgeschlossen):
      - 08_Templates/* — Templates haben absichtlich keine FM-Werte
      - **/README.md, **/_index.md — strukturelle Doku ohne Schema-Pflicht
      - 06_Meta/health-reports/* — automatisch generierte Reports
      - Placeholder-Pattern als wikilink-Targets (`<x>`, single-letter IDs,
        IDs mit Spaces oder Em-Dashes — die sind oft Doku-Beispiele)

    Args:
        include_structural: Wenn True, ALLES scannen (auch Templates/READMEs/Reports).
            Default False — nur "echte" Content-Files.

    Returns:
        Dictionary mit Listen pro Issue-Kategorie + Counts.
    """
    try:
        all_md = vault.walk_md()
        # ID-Index aufbauen
        id_to_paths: dict[str, list[str]] = {}
        contexts_seen: dict[str, int] = {}
        files_data: list[dict[str, Any]] = []
        missing_fm: list[str] = []
        missing_required: list[dict[str, Any]] = []

        # Filter-Functions im Smart-Mode
        import re as _re
        skip_path = lambda rel: not include_structural and (
            rel.startswith("08_Templates/")
            or rel.endswith("/README.md")
            or rel == "README.md"
            or rel.endswith("/_index.md")
            or rel.endswith("/CONTEXT.md")
            or rel == "CLAUDE.md"
            or rel.startswith("06_Meta/health-reports/")
            or rel.startswith("06_Meta/health_checks/")
            or rel.startswith("06_Meta/bot-memory/")
            or rel == "07_Tools/training/system_prompt.md"
            # Schema/Pipelines/MOC sind selbst Doku ohne id-Pflicht
            or rel in ("PIPELINES.md", "SCHEMA.md", "COMMANDS.md", "MOC.md")
            or rel.startswith("06_Meta/todo")
            or rel.startswith("06_Meta/orphans")
            or rel.startswith("06_Meta/stats")
            or rel.startswith("06_Meta/changelog")
            or rel.startswith("99_Archive/")
        )

        # Code-Spans (`...`) und Code-Blöcke (```...```) entfernen bevor wir
        # nach Wikilinks suchen — `[[link]]` in <code>-Tags / inline-Code ist
        # literaler Text, kein echter Link.
        code_block_re = _re.compile(r"```.*?```", _re.DOTALL)
        inline_code_re = _re.compile(r"`[^`\n]+`")
        html_code_re = _re.compile(r"<code>.*?</code>", _re.DOTALL | _re.IGNORECASE)
        def strip_code(text: str) -> str:
            text = code_block_re.sub("", text)
            text = html_code_re.sub("", text)
            text = inline_code_re.sub("", text)
            return text

        # Placeholder-Wikilink-Erkennung: wenn der Target wie ein Doku-Beispiel
        # aussieht, ignorieren (single letter, < > braces, Spaces, Em-Dashes)
        placeholder_re = _re.compile(
            r"^([a-z]|<.*>|\.\.\.|.*\s.*|.*[—–].*|kebab-case-id|t-<slug>|wikilink|filename|"
            r"konzept(-[ab])?|alt|neu|id|x|a|b)$"
        )
        is_placeholder = lambda target: bool(placeholder_re.match(target.lower())) or " " in target

        for path in all_md:
            try:
                text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            except OSError:
                continue
            rel = vault.rel_path(path)
            try:
                post = frontmatter.loads(text)
            except Exception:  # noqa: BLE001
                if not skip_path(rel):
                    missing_fm.append(rel)
                continue
            fm = post.metadata
            if not fm:
                if not skip_path(rel):
                    missing_fm.append(rel)
                continue
            fid = fm.get("id")
            ftype = fm.get("type")
            missing_keys = [k for k in ["id", "type", "title"] if not fm.get(k)]
            if missing_keys and not skip_path(rel):
                missing_required.append({"path": rel, "missing": missing_keys})
            if fid:
                id_to_paths.setdefault(str(fid), []).append(rel)
            if "context" in fm:
                contexts_seen[str(fm["context"])] = contexts_seen.get(str(fm["context"]), 0) + 1
            files_data.append({"path": rel, "id": fid, "body": post.content, "type": ftype})

        # Duplicate IDs
        duplicate_ids = [
            {"id": k, "paths": v} for k, v in id_to_paths.items() if len(v) > 1
        ]

        # Broken wikilinks (Placeholder-Patterns ignorieren + Templates/Reports skip
        # + Code-Spans entfernen damit [[link]] in <code> nicht als Wikilink gilt)
        all_ids = set(id_to_paths.keys())
        broken_wikilinks: list[dict[str, Any]] = []
        for fd in files_data:
            if skip_path(fd["path"]):
                continue
            body_no_code = strip_code(fd["body"] or "")
            broken_in_file: set[str] = set()
            for m in vault._WIKILINK_RE.finditer(body_no_code):
                target = m.group(1).strip()
                if target and target not in all_ids and not is_placeholder(target):
                    broken_in_file.add(target)
            if broken_in_file:
                broken_wikilinks.append({
                    "path": fd["path"],
                    "broken_targets": sorted(broken_in_file),
                })

        # Context-Drift: gleicher Wert mit + ohne @
        context_drift: list[dict[str, Any]] = []
        normalized: dict[str, list[str]] = {}
        for ctx in contexts_seen:
            norm = ctx.lstrip("@").lower()
            normalized.setdefault(norm, []).append(ctx)
        for norm, variants in normalized.items():
            if len(variants) > 1:
                context_drift.append({
                    "normalized": norm,
                    "variants": variants,
                    "counts": {v: contexts_seen[v] for v in variants},
                })

        # Orphans (Notes/Meetings ohne Backlinks, Templates+Archive ausgenommen)
        referenced_ids: set[str] = set()
        for fd in files_data:
            for m in vault._WIKILINK_RE.finditer(fd["body"] or ""):
                referenced_ids.add(m.group(1).strip())
        orphans: list[dict[str, Any]] = []
        for fd in files_data:
            if skip_path(fd["path"]):
                continue
            if fd["type"] in ("note", "meeting") and fd["id"] and str(fd["id"]) not in referenced_ids:
                orphans.append({"path": fd["path"], "id": fd["id"], "type": fd["type"]})

        return {
            "summary": {
                "total_md_files": len(files_data),
                "broken_wikilink_files": len(broken_wikilinks),
                "duplicate_ids": len(duplicate_ids),
                "missing_fm": len(missing_fm),
                "missing_required": len(missing_required),
                "context_drift": len(context_drift),
                "orphans": len(orphans),
                "scan_mode": "all" if include_structural else "smart (templates+readmes+reports skip)",
            },
            "broken_wikilinks": broken_wikilinks[:50],
            "duplicate_ids": duplicate_ids,
            "missing_fm": missing_fm[:50],
            "missing_required": missing_required[:50],
            "context_drift": context_drift,
            "orphans": orphans[:50],
        }
    except VaultError as e:
        raise ToolError(str(e))


@mcp.tool()
def self_test() -> dict[str, Any]:
    """Self-Test — prüft jedes Tool funktional + misst Latenz.

    Read-only Aufrufe gegen den eigenen Server. Ergebnis ist ein Health-Report
    mit per-Tool-Status + Total-Latenz. Kein Vault-Write.

    Returns:
        {tools: [{name, ok, latency_ms, error?}], total_ms, ok_count, fail_count}
    """
    import time as _t
    results: list[dict[str, Any]] = []
    start_total = _t.perf_counter()

    def run(name: str, callable_) -> None:
        s = _t.perf_counter()
        try:
            r = callable_()
            ok = isinstance(r, dict) and "error" not in r
            results.append({
                "name": name,
                "ok": ok,
                "latency_ms": round((_t.perf_counter() - s) * 1000, 2),
                **({"error": r.get("error")} if not ok and isinstance(r, dict) else {}),
            })
        except Exception as e:  # noqa: BLE001
            results.append({
                "name": name,
                "ok": False,
                "latency_ms": round((_t.perf_counter() - s) * 1000, 2),
                "error": f"{type(e).__name__}: {e}",
            })

    # Direkte Helper-Calls (umgehen Audit damit Self-Test sich nicht selbst loggt
    # bzw. nur dieses einzelne Event)
    run("vault_root_exists", lambda: {"ok": True} if vault.VAULT_PATH.exists() else {"error": "vault missing"})
    run("list_root", lambda: {"path": "", "entries": vault.list_dir("")})
    run("list_tasks_dir", lambda: {"path": "10_Life/tasks", "entries": vault.list_dir("10_Life/tasks")})
    run("read_schema", lambda: {"raw": vault.read_text("SCHEMA.md")[:50]})
    run("search_basic", lambda: {"hits": vault.grep_vault("matura", max_results=1)})
    run("today_iso", lambda: {"date": vault.today_iso()})
    run("snapshot_dir_writable", lambda: (
        {"ok": True} if (snapshot.SNAPSHOT_DIR.exists() or not snapshot.SNAPSHOT_ENABLED)
        else {"error": f"snapshot dir missing: {snapshot.SNAPSHOT_DIR}"}
    ))

    total_ms = round((_t.perf_counter() - start_total) * 1000, 2)
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "tools": results,
        "ok_count": ok_count,
        "fail_count": len(results) - ok_count,
        "total_ms": total_ms,
        "vault": str(vault.VAULT_PATH),
        "snapshot_enabled": snapshot.SNAPSHOT_ENABLED,
    }


# ---------- Maintain-State (in-memory) -----------------------------------
# Letzter Maintain-Run für Health-Endpoint + Anti-Spam (nicht 2× parallel)
_last_maintain: dict[str, Any] = {
    "started_at": None,
    "finished_at": None,
    "duration_ms": 0,
    "status": "never",  # never | ok | error | running
    "summary": {},
}


def _run_maintain_locked() -> dict[str, Any]:
    """Wrapper: maintain.run_maintain mit Lock damit nicht parallel läuft."""
    if _last_maintain["status"] == "running":
        return {"skipped": "already_running"}
    _last_maintain["status"] = "running"
    _last_maintain["started_at"] = maintain.datetime.now().isoformat(timespec="seconds")
    try:
        report = maintain.run_maintain()
        _last_maintain["finished_at"] = report["finished_at"]
        _last_maintain["duration_ms"] = report["duration_ms"]
        _last_maintain["summary"] = {
            k: ("error" if isinstance(v, dict) and "error" in v else "ok")
            for k, v in report.get("steps", {}).items()
        }
        _last_maintain["status"] = "ok" if all(
            v == "ok" for v in _last_maintain["summary"].values()
        ) else "error"
        return report
    except Exception as e:  # noqa: BLE001
        _last_maintain["status"] = "error"
        _last_maintain["summary"] = {"exception": str(e)}
        log.error("maintain run failed: %s", e)
        raise ToolError(str(e))


@mcp.tool()
def vault_maintain() -> dict[str, Any]:
    """Führt die komplette Vault-Self-Maintenance-Pipeline aus.

    Pipeline-Schritte (idempotent, Errors werden gefangen):
      1. Auto-Link alle Projekt-READMEs (Notes/Meetings)
      2. Context-Drift normalisieren (@home → home in Tasks)
      3. Daily-Backlinks: heute erstellte Items in heutige Daily-Note
      4. Recurring-Tasks reaktivieren (daily/weekdays/weekly/monthly fällig)
      5. Daily-Skeleton für morgen pre-create (idempotent)
      6. Goal-Status-Check (Säulen-Drift, Habits/Sport-Score)
      7. Lint-Summary (was nicht auto-fixbar war)

    Wird automatisch getriggert:
      - Nach jedem Schreibvorgang (immediate, async)
      - Periodisch alle 10 Minuten (background scheduler)
      - Beim Server-Start (boot-run)

    Plus jederzeit manuell via Tool-Call.

    Returns:
        Pipeline-Report mit pro-Schritt Status + Counts + Errors.
    """
    return _run_maintain_locked()


@mcp.tool()
def task_reactivate_recurring() -> dict[str, Any]:
    """Reaktiviert fällige recurring Tasks (daily/weekdays/weekly/monthly).

    Walked alle Tasks in 10_Life/tasks/, sucht status=done mit recurrence-FM
    und fälligem Pattern (z.B. daily Tasks deren last_completed gestern war).
    Setzt status zurück auf 'open', appendet "- YYYY-MM-DD: reaktiviert"
    ans Body-Ende.

    Idempotent: re-run findet schon-reaktivierte Tasks (status=open) und
    skippt sie. Timezone-aware: nutzt Wien-Zeit damit nicht nach 22:00
    fälschlich in den nächsten Tag übergesprungen wird.

    Output: {checked: int, reactivated: [slug, ...], count: int, errors: [...]}
    """
    return maintain.step_task_reactivate_recurring()


@mcp.tool()
def create_daily_skeleton(date: str | None = None) -> dict[str, Any]:
    """Erstellt eine Daily-Note (mit FM-Skeleton) für ein Datum.

    Default: morgen (Wien-Zeit) — damit du jeden Morgen schon ein leeres
    Daily-Template hast.

    Args:
        date: ISO-Datum (YYYY-MM-DD). Default = morgen Wien-Zeit.

    Idempotent (TOCTOU-safe via O_EXCL): wenn die Datei schon existiert,
    wird sie NICHT überschrieben.

    Output: {date, path, created: bool, reason?}
    """
    return maintain.step_create_daily_skeleton(date)


@mcp.tool()
def goal_status_check() -> dict[str, Any]:
    """Read-only Status-Check des 5y-Goal-Systems.

    Liefert:
      - drift: Status pro Anker-Bucket (weekly/monthly/quarterly) mit
        age_days und ok/warn (Schwellen: 14/60/120 Tage).
      - habits_7d: check/possible/pct + ok/warn (Soll: ≥80%)
      - sport: d7/d30 Sessions + ok/warn (Wochen-Soll: 3)
      - overall: ok/warn (warn wenn IRGENDEIN Bucket warn ist)

    Schreibt nichts — pure Diagnose. Nutzbar für Bot-Briefing oder
    Dashboard-Indicator.

    Output: {date, drift, habits_7d, sport, overall}
    """
    return maintain.step_goal_status_check()


@mcp.tool()
def vault_autolink(dry_run: bool = False) -> dict[str, Any]:
    """Linkt automatisch alle Notes/Meetings unter 05_Projects/<slug>/ in deren
    Projekt-README.md.

    Erstellt/Aktualisiert einen Auto-Block zwischen Markern:
        <!-- AUTO-NOTES-START -->
        ## Notes
        - `2026-05-04` [[note-id|Note Title]]
        ## Meetings
        - ...
        <!-- AUTO-NOTES-END -->

    Manueller README-Inhalt vor/nach den Markern bleibt 100% erhalten.

    Idempotent: kann jederzeit re-run werden ohne Drift zu verursachen.
    Auto-läuft nach jedem create_note/create_meeting mit project=<slug>.

    Args:
        dry_run: Wenn True, zeigt nur welche READMEs aktualisiert würden.

    Returns:
        {updated: [{project, notes_count, meetings_count}], total_updated, dry_run}
    """
    try:
        projects_dir = vault.VAULT_PATH / "05_Projects"
        if not projects_dir.is_dir():
            return {"updated": [], "total_updated": 0, "dry_run": dry_run}

        updated: list[dict[str, Any]] = []
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            readme = project_dir / "README.md"
            if not readme.is_file():
                continue
            sections = vault.collect_project_content(project_dir.name)
            total_items = sum(len(v) for v in sections.values())
            if total_items == 0:
                continue

            if dry_run:
                # Konservativ: jeder Project-Folder zaehlt als "would_change".
                # Echtes Diff-vs-Renderresult ist Aufwand fuer wenig Mehrwert in
                # einem dry-run-Modus der eh nur diagnostisch ist.
                updated.append({
                    "project": project_dir.name,
                    "notes": len(sections.get("Notes", [])),
                    "meetings": len(sections.get("Meetings", [])),
                    "other": len(sections.get("Sonstige", [])),
                    "would_change": True,
                })
            else:
                changed = vault.update_auto_notes_block(readme, sections)
                if changed:
                    updated.append({
                        "project": project_dir.name,
                        "notes": len(sections.get("Notes", [])),
                        "meetings": len(sections.get("Meetings", [])),
                        "other": len(sections.get("Sonstige", [])),
                    })

        return {
            "updated": updated,
            "total_updated": len(updated),
            "dry_run": dry_run,
        }
    except VaultError as e:
        raise ToolError(str(e))


# ---------- Phase-X4 Query-Tools --------------------------------------------
# Read-only Lookup-Tools fuer Vault-Inhalts-Modell:
# Backlinks, Outgoing-Links, Tags, Frontmatter-Properties, Aliases, Outline.
# Alle haben readOnlyHint=True.


@mcp.tool()
def get_backlinks(path: str, scope: str | None = None) -> dict[str, Any]:
    """Findet alle Files die auf `path` linken.

    Erkennt:
      - Wikilinks `[[<id>]]`, `[[<id>|display]]`, `[[<id>#anchor]]`
      - Frontmatter `related: [<id>, ...]`

    `path → id`-Resolution: Frontmatter `id`-Field zuerst, sonst filename
    ohne `.md`. Wenn `path` selbst nicht existiert: trotzdem versuchen
    (filename-fallback) — sinnvoll fuer Lookup nach geloeschten IDs.

    Args:
        path: Rel-Pfad zur Ziel-Datei (oder direkt eine ID-aehnliche Form)
        scope: Optional auf Subfolder beschraenken (z.B. `05_Projects`)

    Returns:
        {target_id, total, hits: [{path, lines, via}]}
        via ∈ {"wikilink", "related"}
    """
    if err := validators.validate_get_backlinks(path, scope):
        raise ToolError(err)

    target_id = vault.file_id(path) or path
    target_id = target_id.strip()
    if not target_id:
        raise ToolError("Kann ID aus Pfad nicht ableiten")

    hits: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    # Wikilink-Refs
    for p, line_nums in vault.find_wikilink_refs(target_id):
        rel = vault.rel_path(p)
        if scope and not rel.startswith(scope.rstrip("/") + "/"):
            continue
        seen_paths.add(rel)
        hits.append({"path": rel, "lines": line_nums, "via": "wikilink"})

    # Related-Refs (Frontmatter)
    for p in vault.find_related_refs(target_id):
        rel = vault.rel_path(p)
        if scope and not rel.startswith(scope.rstrip("/") + "/"):
            continue
        if rel in seen_paths:
            # Gleiches File via wikilink AND related — als kombiniert markieren
            for h in hits:
                if h["path"] == rel:
                    h["via"] = "wikilink+related"
                    break
        else:
            hits.append({"path": rel, "lines": [], "via": "related"})

    hits.sort(key=lambda h: h["path"])
    return {"target_id": target_id, "total": len(hits), "hits": hits}


@mcp.tool()
def get_outgoing_links(path: str) -> dict[str, Any]:
    """Listet alle Wikilinks im Body von `path`.

    Aufgeloest:
      - resolved_path: Pfad im Vault wenn ID gefunden wird
      - resolved=False bei broken Links

    Args:
        path: Rel-Pfad zur Quell-Datei (.md)

    Returns:
        {path, total, links: [{target_id, resolved, resolved_path?}]}
    """
    if err := validators.validate_get_outgoing_links(path):
        raise ToolError(err)
    try:
        post = vault.read_post(path)
    except VaultError as e:
        raise ToolError(str(e))

    targets = vault.parse_wikilink_targets(post.content)

    # ID-Index aufbauen (id -> rel_path) fuer Aufloesung
    id_index: dict[str, str] = {}
    for p in vault.walk_md():
        try:
            tp = vault.read_post(vault.rel_path(p))
        except (VaultError, OSError):
            continue
        fm_id = tp.metadata.get("id")
        if fm_id:
            id_index[str(fm_id).strip()] = vault.rel_path(p)
        # Filename-fallback (low priority — nur wenn kein FM-id existiert)
        stem = p.stem
        if stem and stem not in id_index:
            id_index[stem] = vault.rel_path(p)

    links: list[dict[str, Any]] = []
    for tid in targets:
        resolved = tid in id_index
        entry: dict[str, Any] = {"target_id": tid, "resolved": resolved}
        if resolved:
            entry["resolved_path"] = id_index[tid]
        links.append(entry)

    return {"path": path, "total": len(links), "links": links}


@mcp.tool()
def list_tags(scope: str | None = None, min_count: int = 1) -> dict[str, Any]:
    """Tag-Index ueber den Vault. Counts pro Tag, sortiert.

    Erfasst:
      - Frontmatter `tags: [...]` (Liste oder String)
      - Inline `#tag` im Body (Code-Blocks ausgeschlossen)

    Args:
        scope: Optional auf Subfolder beschraenken
        min_count: Tags mit weniger Vorkommen werden ausgeblendet (default 1)

    Returns:
        {scope, total_unique, tags: [{tag, count, sources: {fm: int, inline: int}}]}
    """
    if err := validators.validate_list_tags(scope, min_count):
        raise ToolError(err)

    counts: dict[str, dict[str, int]] = {}
    try:
        files = vault.walk_md(scope or "")
    except VaultError as e:
        raise ToolError(str(e))

    for p in files:
        try:
            post = vault.read_post(vault.rel_path(p))
        except (VaultError, OSError):
            continue
        # Frontmatter tags
        fm_tags = post.metadata.get("tags") or []
        if isinstance(fm_tags, str):
            fm_tags = [fm_tags]
        if isinstance(fm_tags, list):
            for t in fm_tags:
                if isinstance(t, str) and t.strip():
                    name = t.strip().lstrip("#")
                    counts.setdefault(name, {"fm": 0, "inline": 0})["fm"] += 1
        # Inline tags
        for t in vault.parse_inline_tags(post.content):
            counts.setdefault(t, {"fm": 0, "inline": 0})["inline"] += 1

    out = []
    for name, src in counts.items():
        total = src["fm"] + src["inline"]
        if total < min_count:
            continue
        out.append({"tag": name, "count": total, "sources": src})
    out.sort(key=lambda x: (-x["count"], x["tag"]))
    return {"scope": scope or "", "total_unique": len(out), "tags": out}


@mcp.tool()
def find_by_tag(tag: str, scope: str | None = None) -> dict[str, Any]:
    """Findet alle Files mit `tag` (Frontmatter ODER Inline).

    Args:
        tag: Tag-Name mit oder ohne `#`-Praefix (`#urgent` oder `urgent`)
        scope: Optional auf Subfolder beschraenken

    Returns:
        {tag, total, hits: [{path, via}]}
        via ∈ {"frontmatter", "inline", "frontmatter+inline"}
    """
    if err := validators.validate_find_by_tag(tag, scope):
        raise ToolError(err)

    needle = tag.lstrip("#").strip()
    try:
        files = vault.walk_md(scope or "")
    except VaultError as e:
        raise ToolError(str(e))

    hits: list[dict[str, Any]] = []
    for p in files:
        try:
            post = vault.read_post(vault.rel_path(p))
        except (VaultError, OSError):
            continue
        fm_tags = post.metadata.get("tags") or []
        if isinstance(fm_tags, str):
            fm_tags = [fm_tags]
        in_fm = isinstance(fm_tags, list) and any(
            isinstance(t, str) and t.lstrip("#").strip() == needle for t in fm_tags
        )
        in_body = needle in vault.parse_inline_tags(post.content)
        if not (in_fm or in_body):
            continue
        via = "frontmatter+inline" if (in_fm and in_body) else (
            "frontmatter" if in_fm else "inline"
        )
        hits.append({"path": vault.rel_path(p), "via": via})

    hits.sort(key=lambda h: h["path"])
    return {"tag": needle, "total": len(hits), "hits": hits}


@mcp.tool()
def find_by_property(
    field: str,
    value: Any = None,
    op: str = "eq",
    scope: str | None = None,
) -> dict[str, Any]:
    """Sucht Files mit Frontmatter-Property `field` <op> `value`.

    Operations:
      - eq: field == value  (string-vergleich, case-sensitive)
      - contains: value in field (string substring oder list-contains)
      - gt / lt: numerisch oder ISO-Date-Vergleich
      - exists: field ist im Frontmatter gesetzt (value=None)
      - in: field-Wert ∈ value-Liste

    Args:
        field: Frontmatter-Feldname (z.B. "status", "priority", "due")
        value: Vergleichswert (None bei op='exists')
        op: Vergleichs-Operator (default 'eq')
        scope: Optional Subfolder

    Returns:
        {field, op, value, total, hits: [{path, value: <gefundener Wert>}]}
    """
    if err := validators.validate_find_by_property(field, value, op, scope):
        raise ToolError(err)
    try:
        files = vault.walk_md(scope or "")
    except VaultError as e:
        raise ToolError(str(e))

    def _match(actual: Any) -> bool:
        if op == "exists":
            return True  # nur erreichbar wenn field im FM
        if op == "eq":
            return str(actual) == str(value)
        if op == "contains":
            if isinstance(actual, list):
                return any(str(value) in str(x) for x in actual)
            return str(value) in str(actual)
        if op == "in":
            return str(actual) in {str(v) for v in value}
        if op in ("gt", "lt"):
            try:
                a_num = float(actual)
                v_num = float(value)
                return (a_num > v_num) if op == "gt" else (a_num < v_num)
            except (TypeError, ValueError):
                # String-Vergleich (ISO-Dates lexikografisch korrekt)
                return (str(actual) > str(value)) if op == "gt" else (str(actual) < str(value))
        return False

    hits: list[dict[str, Any]] = []
    for p in files:
        try:
            post = vault.read_post(vault.rel_path(p))
        except (VaultError, OSError):
            continue
        if field not in post.metadata:
            continue
        actual = post.metadata[field]
        if not _match(actual):
            continue
        # Komplexe Werte als String fuer Output (kein None/dict-Drift)
        out_val = actual if isinstance(actual, (str, int, float, bool, list)) else str(actual)
        hits.append({"path": vault.rel_path(p), "value": out_val})

    hits.sort(key=lambda h: h["path"])
    return {
        "field": field, "op": op, "value": value,
        "total": len(hits), "hits": hits,
    }


@mcp.tool()
def resolve_alias(query: str, scope: str | None = None) -> dict[str, Any]:
    """Sucht Files deren Frontmatter `aliases: [...]` `query` enthaelt.

    Match-Logik (case-insensitive):
      - exact: alias == query
      - substring: query in alias

    Returns Treffer sortiert: exact-matches zuerst.

    Args:
        query: Such-String (z.B. ein Spitzname, alternativer Name)
        scope: Optional Subfolder

    Returns:
        {query, total, hits: [{path, alias_matched, match_type, id?, title?}]}
        match_type ∈ {"exact", "substring"}
    """
    if err := validators.validate_resolve_alias(query, scope):
        raise ToolError(err)
    needle = query.strip().lower()
    try:
        files = vault.walk_md(scope or "")
    except VaultError as e:
        raise ToolError(str(e))

    hits: list[dict[str, Any]] = []
    for p in files:
        try:
            post = vault.read_post(vault.rel_path(p))
        except (VaultError, OSError):
            continue
        aliases = post.metadata.get("aliases")
        if not isinstance(aliases, list):
            continue
        for a in aliases:
            if not isinstance(a, str):
                continue
            a_low = a.strip().lower()
            if not a_low:
                continue
            if a_low == needle:
                match_type = "exact"
            elif needle in a_low:
                match_type = "substring"
            else:
                continue
            hits.append({
                "path": vault.rel_path(p),
                "alias_matched": a,
                "match_type": match_type,
                "id": str(post.metadata.get("id", "")) or None,
                "title": str(post.metadata.get("title", "")) or None,
            })
            break  # ein Hit pro File reicht

    # Exact-matches zuerst, dann nach Path
    hits.sort(key=lambda h: (0 if h["match_type"] == "exact" else 1, h["path"]))
    return {"query": query, "total": len(hits), "hits": hits}


@mcp.tool()
def get_outline(path: str, include_tables: bool = False) -> dict[str, Any]:
    """Strukturelles Outline einer Markdown-Datei.

    Token-Saver bei grossen Files: LLM weiss welche Sections existieren,
    ohne den ganzen Body zu lesen. Plus Tabellen-Header optional damit der
    LLM `append_table_row` informiert aufrufen kann.

    Args:
        path: Rel-Pfad zur .md-Datei
        include_tables: Wenn True, auch Tabellen-Header (mit Spalten) im Outline

    Returns:
        {path, headings: [{level, text, line}],
         tables?: [{line, columns: [str], n_data_rows}]}
    """
    if err := validators.validate_get_outline(path, include_tables):
        raise ToolError(err)
    try:
        post = vault.read_post(path)
    except VaultError as e:
        raise ToolError(str(e))

    body = post.content
    result: dict[str, Any] = {
        "path": path,
        "headings": vault.extract_headings(body),
    }

    if include_tables:
        # Tabellen finden via existierender Helper aus append_table_row-Block
        lines = body.split("\n")
        tables: list[dict[str, Any]] = []
        i = 0
        while i < len(lines) - 1:
            if _is_table_row(lines[i]) and _is_table_separator(lines[i + 1]):
                cols_raw = lines[i].strip()[1:-1].split("|")
                cols = [c.strip() for c in cols_raw]
                # Daten-Zeilen zaehlen
                j = i + 2
                n_data = 0
                while j < len(lines) and _is_table_row(lines[j]):
                    s = lines[j].strip()
                    cells = [c.strip() for c in s[1:-1].split("|")]
                    if not all(c == "" for c in cells):
                        n_data += 1
                    j += 1
                tables.append({"line": i + 1, "columns": cols, "n_data_rows": n_data})
                i = j
            else:
                i += 1
        result["tables"] = tables

    return result


@mcp.tool()
def raw_write(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Schreibt rohen Text in ein File (ohne Frontmatter, ohne Schema).

    Für Files die NICHT dem Note/Task/Meeting-Schema folgen (CONTEXT.md,
    README.md, JSON, txt, etc.). Pfad-Sicherheit greift wie bei allen Tools.

    Args:
        path: Rel-Pfad ab Vault-Root
        content: Vollständiger File-Inhalt (ersetzt bestehenden komplett)
        overwrite: True um existierende Files zu überschreiben (default False)

    Returns:
        {path, bytes_written, overwritten: bool}
    """
    try:
        full = vault.safe_path(path)
        existed = full.exists()
        if existed and not overwrite:
            raise ToolError(f"Datei existiert ({path}). overwrite=true setzen um zu ersetzen.")
        if existed:
            # Snapshot vor Overwrite
            try:
                snapshot.snapshot_path(path, full.read_bytes(), "raw_write")
            except OSError:
                pass
        full.parent.mkdir(parents=True, exist_ok=True)
        # CRLF → LF zur Konsistenz mit anderen Tools
        normalized = content.replace("\r\n", "\n")
        full.write_text(normalized, encoding="utf-8")
        return {
            "path": path,
            "bytes_written": len(normalized.encode("utf-8")),
            "overwritten": existed,
        }
    except VaultError as e:
        raise ToolError(str(e))


# ---------- Auth Middleware --------------------------------------------------


class DualAuthMiddleware(BaseHTTPMiddleware):
    """Dual-Auth: OAuth 2.1 JWT (bevorzugt) + statischer Bearer-Token (legacy).

    Validierung:
      1. JWT-Format-Detection (header.payload.signature → 3 Punkte) → OAuth-Validation
      2. Sonst → statischer Bearer-Token-Match wie bisher

    Ausnahmen (kein Auth-Check):
      - GET /health
      - GET /.well-known/oauth-* (Discovery muss public sein)
      - GET/POST /oauth/* (OAuth-Server-Endpoints sind via PKCE+Login geschuetzt)

    Bei 401: WWW-Authenticate-Header weist auf Auth-Server hin (RFC 9728).
    """

    PUBLIC_PATHS = ("/health", "/.well-known/", "/oauth/")

    def _is_public(self, path: str) -> bool:
        return path == "/health" or any(path.startswith(p) for p in self.PUBLIC_PATHS)

    def _www_authenticate(self) -> str:
        # Spec-konformer Hint: Client soll das resource-metadata-doc holen
        return (
            'Bearer realm="ki-os-mcp", '
            f'resource_metadata="{oauth.OAUTH_RESOURCE.rstrip("/")}/.well-known/oauth-protected-resource"'
        )

    async def dispatch(self, request: Request, call_next):
        if self._is_public(request.url.path):
            return await call_next(request)

        # Dev-Mode: keine Tokens konfiguriert → durchwinken (mit Warning bereits beim Boot)
        if not _VALID_TOKENS and not oauth.is_configured():
            return await call_next(request)

        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        ua = request.headers.get("user-agent", "")[:120]

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            audit.log_auth(False, client_ip, "Missing Bearer prefix", ua)
            return JSONResponse(
                {"error": "Missing Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": self._www_authenticate()},
            )
        token = auth.removeprefix("Bearer ").strip()

        # JWT-Format-Detection: 3 base64url-segments durch Punkte getrennt
        if oauth.is_configured() and token.count(".") == 2:
            claims = oauth.verify_access_token(token)
            if claims is not None:
                # OAuth-Pfad: gueltig
                request.state.auth_subject = claims.get("sub")
                request.state.auth_client_id = claims.get("client_id")
                request.state.auth_method = "oauth"
                return await call_next(request)
            # JWT-Format aber ungueltig → loggen + 401
            audit.log_auth(False, client_ip, "Invalid JWT", ua)
            return JSONResponse(
                {"error": "invalid_token", "error_description": "JWT abgelaufen oder Signatur falsch"},
                status_code=401,
                headers={"WWW-Authenticate": self._www_authenticate()},
            )

        # Fallback: statischer Bearer-Token-Match (legacy)
        if not _VALID_TOKENS:
            audit.log_auth(False, client_ip, "No static tokens configured", ua)
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": self._www_authenticate()},
            )
        if token not in _VALID_TOKENS:
            audit.log_auth(False, client_ip, "Invalid token", ua)
            return JSONResponse(
                {"error": "Invalid token"},
                status_code=401,
                headers={"WWW-Authenticate": self._www_authenticate()},
            )
        if MCP_TOKEN_LEGACY and token == MCP_TOKEN_LEGACY:
            audit.log_auth(True, client_ip, "legacy token", ua)
        request.state.auth_method = "bearer"
        return await call_next(request)


# Alt-Name fuer Backward-Compat falls andere Module den Namen referenzieren
BearerAuthMiddleware = DualAuthMiddleware


# ---------- Health Endpoint --------------------------------------------------


_BOOT_TIME = None


async def health(_: Request) -> JSONResponse:
    from ki_os_mcp import __version__
    import time as _t
    global _BOOT_TIME
    if _BOOT_TIME is None:
        _BOOT_TIME = _t.time()
    try:
        tools_count = len(mcp._tool_manager._tools)  # type: ignore[attr-defined]
    except AttributeError:
        tools_count = 0
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "vault": str(vault.VAULT_PATH),
            "vault_exists": vault.VAULT_PATH.exists(),
            "auth": "enabled" if _VALID_TOKENS else "DISABLED",
            "auth_legacy_active": bool(MCP_TOKEN_LEGACY),
            "oauth": {
                "enabled": oauth.is_configured(),
                "issuer": oauth.OAUTH_ISSUER if oauth.is_configured() else None,
                "user": oauth.OAUTH_USER_EMAIL if oauth.is_configured() else None,
                "access_token_ttl": oauth.ACCESS_TOKEN_TTL,
                "refresh_token_ttl": oauth.REFRESH_TOKEN_TTL,
            },
            "tools": tools_count,
            "uptime_seconds": int(_t.time() - _BOOT_TIME),
            "rate_limit_per_min": int(os.environ.get("MCP_RATE_LIMIT_PER_MIN", "60")),
            "snapshot_enabled": os.environ.get("MCP_SNAPSHOT_ENABLED", "1") not in ("0", "false", "no"),
            "spec_compliance": {
                "origin_validation": True,           # Spec MUST §Transports
                "tool_error_isError": True,           # Spec §Tools error-handling
                "tool_annotations_set": len(TOOL_ANNOTATIONS),
                "allowed_hosts": len(ALLOWED_HOSTS),
                "allowed_origins": len(ALLOWED_ORIGINS),
            },
            "maintain": {
                "status": _last_maintain["status"],
                "last_run": _last_maintain["finished_at"],
                "duration_ms": _last_maintain["duration_ms"],
                "steps": _last_maintain["summary"],
            },
        }
    )


# ---------- App-Wiring -------------------------------------------------------


def _wrap_tools_with_audit() -> None:
    """Wrap alle registrierten FastMCP-Tools mit dem Audit-Logger.

    Jeder Tool-Call landet als JSONL-Line in MCP_AUDIT_LOG.
    """
    # FastMCP._tool_manager._tools : dict[name, Tool]
    try:
        tools = mcp._tool_manager._tools  # type: ignore[attr-defined]
    except AttributeError:
        log.warning("Konnte FastMCP-Tools nicht für Audit wrappen")
        return
    wrapped = 0
    for name, tool in tools.items():
        original_fn = tool.fn  # type: ignore[attr-defined]
        if getattr(original_fn, "_audit_wrapped", False):
            continue
        wrapped_fn = audit.time_call(original_fn)
        wrapped_fn._audit_wrapped = True  # type: ignore[attr-defined]
        wrapped_fn.__name__ = name  # für Audit-Log
        tool.fn = wrapped_fn  # type: ignore[attr-defined]
        wrapped += 1
    log.info("Audit-Wrapper aktiv für %d Tools", wrapped)


# ---------- Tool-Annotations (MCP-Spec §Tools) -------------------------------
# Annotations geben Clients (Claude Code, claude.ai) Hints fuer das Trust&Safety-
# UI: read-only Tools koennen auto-approved werden, destructive bekommen
# Confirmation-Prompt, idempotent zeigt "kann sicher wiederholt werden".
#
# Hint: Spec sagt Annotations sind UNTRUSTED metadata — Client darf nicht blind
# vertrauen, dass `readOnlyHint:true` heisst dass Tool wirklich read-only ist.
# Sie sind UI-Hilfe, kein Sicherheitsmechanismus. Echter Schutz: Bearer-Auth +
# strikte Server-side-Validation (haben wir).

TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    # --- Read-only ---
    "search_vault":         {"readOnlyHint": True, "title": "Vault-Volltextsuche"},
    "read_file":            {"readOnlyHint": True, "title": "Datei lesen"},
    "list_files":           {"readOnlyHint": True, "title": "Folder-Inhalt listen"},
    "list_tasks":           {"readOnlyHint": True, "title": "Tasks listen"},
    "daily_briefing":       {"readOnlyHint": True, "title": "Tagesbriefing"},
    "self_test":            {"readOnlyHint": True, "title": "Server-Self-Test"},
    "read_vision":          {"readOnlyHint": True, "title": "5y-Vision lesen"},
    "read_saeulen":         {"readOnlyHint": True, "title": "Saeulen-Tabelle lesen"},
    "read_drift":           {"readOnlyHint": True, "title": "Drift-Anker lesen"},
    "read_habits":          {"readOnlyHint": True, "title": "Habits lesen"},
    "read_sport":           {"readOnlyHint": True, "title": "Sport-Sessions lesen"},
    "read_books":           {"readOnlyHint": True, "title": "Buecher lesen"},
    "read_wins":            {"readOnlyHint": True, "title": "Wins lesen"},
    "compute_streak":       {"readOnlyHint": True, "title": "Habit-Streak berechnen"},
    "read_reminders":       {"readOnlyHint": True, "title": "Reminders lesen"},
    "read_yesterday_daily": {"readOnlyHint": True, "title": "Gestrige Daily lesen"},
    "goal_status_check":    {"readOnlyHint": True, "title": "Goal-Status-Check"},
    "vault_lint":           {"readOnlyHint": True, "title": "Vault-Schema-Linter"},
    # --- Phase-X4 Query-Tools (read-only Vault-Inhalts-Modell) ---
    "get_backlinks":        {"readOnlyHint": True, "title": "Backlinks finden"},
    "get_outgoing_links":   {"readOnlyHint": True, "title": "Ausgehende Links"},
    "list_tags":            {"readOnlyHint": True, "title": "Tag-Index"},
    "find_by_tag":          {"readOnlyHint": True, "title": "Files nach Tag finden"},
    "find_by_property":     {"readOnlyHint": True, "title": "Files nach Frontmatter-Property"},
    "resolve_alias":        {"readOnlyHint": True, "title": "Alias aufloesen"},
    "get_outline":          {"readOnlyHint": True, "title": "Heading-Outline einer Datei"},
    # --- Write (creates new content, kein destructive Update) ---
    "create_note":          {"title": "Note anlegen"},
    "create_task":          {"title": "Task anlegen"},
    "create_meeting":       {"title": "Meeting anlegen"},
    "create_project":       {"title": "Projekt-Container anlegen"},
    "append_to_daily":      {"title": "An Daily-Note anhaengen"},
    "goal_log":             {"title": "Goal-Log Eintrag"},
    "project_context":      {"title": "Projekt-Kontext setzen"},
    "raw_write":            {"destructiveHint": True, "title": "Raw File-Write (kann ueberschreiben)"},
    # --- Edit (modifies existing) ---
    "edit_file":            {"title": "Datei editieren"},
    "edit_file_replace":    {"title": "Find/Replace im File"},
    "append_table_row":     {"title": "Tabellen-Zeile anhaengen"},
    "task":                 {"title": "Task-Aktion (done/reopen/snooze/edit)"},
    "move":                 {"destructiveHint": True, "title": "Datei verschieben (mit Wikilink-Migration)"},
    "move_bulk":            {"destructiveHint": True, "title": "Bulk-Move mehrerer Files"},
    "move_project":         {"destructiveHint": True, "title": "Projekt-Folder verschieben/nesten"},
    # --- Read project meta ---
    "read_project_context": {"readOnlyHint": True, "title": "Projekt-CONTEXT.md lesen"},
    # --- Delete (2-step pattern) ---
    "request_delete":       {"destructiveHint": True, "title": "Loesch-Anfrage (Stufe 1)"},
    "confirm_delete":       {"destructiveHint": True, "title": "Loeschen bestaetigen (Stufe 2)"},
    # --- Maintain (idempotent — wiederholbar ohne Drift) ---
    "vault_autolink":          {"idempotentHint": True, "title": "Auto-Linking refresh"},
    "vault_maintain":          {"idempotentHint": True, "title": "Self-Maintenance Pipeline"},
    "task_reactivate_recurring": {"idempotentHint": True, "title": "Recurring-Tasks reaktivieren"},
    "create_daily_skeleton":   {"idempotentHint": True, "title": "Daily-Skeleton vorbereiten"},
}


def _set_tool_annotations() -> None:
    """Setzt Annotations auf alle registrierten Tools.

    Best-effort: wenn das offizielle SDK das `annotations`-Feld nicht
    expose't (Issue #511), wird ein Warning geloggt aber Tools laufen
    weiter. Annotations sind nur UX-Hint, kein funktional-kritisches
    Feature.
    """
    try:
        from mcp.types import ToolAnnotations
    except ImportError:
        log.warning("mcp.types.ToolAnnotations nicht verfuegbar — Tool-Annotations skipped")
        return
    try:
        tools = mcp._tool_manager._tools  # type: ignore[attr-defined]
    except AttributeError:
        log.warning("Tool-Manager nicht zugaenglich fuer Annotations")
        return

    set_count = 0
    missing: list[str] = []
    for name, hints in TOOL_ANNOTATIONS.items():
        if name not in tools:
            missing.append(name)
            continue
        try:
            tools[name].annotations = ToolAnnotations(**hints)
            set_count += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Annotation-Fail %s: %s", name, e)

    # Warne bei Tools ohne Annotation (Drift-Schutz)
    annotated = set(TOOL_ANNOTATIONS) & set(tools.keys())
    unannotated = set(tools.keys()) - annotated
    if unannotated:
        log.warning("Tools ohne Annotation: %s", sorted(unannotated))
    if missing:
        log.warning("Annotation fuer fehlendes Tool: %s", sorted(missing))

    log.info("Tool-Annotations gesetzt: %d/%d", set_count, len(tools))


def create_app() -> Starlette:
    """Mount MCP's Streamable-HTTP transport on /mcp + health on /health.

    Middleware-Reihenfolge (outermost zuerst):
      1. RateLimit  → blockt Brute-Force/DoS (429)
      2. BearerAuth → 401 wenn Token fehlt/falsch
      3. (MCP-Handler oder /health-Route)

    Delegiert lifespan an mcp_app damit dessen session_manager initialisiert.
    """
    _wrap_tools_with_audit()
    _set_tool_annotations()
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        audit.log_event("server_start", host=MCP_HOST, port=MCP_PORT,
                        auth=bool(_VALID_TOKENS), legacy=bool(MCP_TOKEN_LEGACY))

        # Background-Scheduler: alle 10 Min Maintain + 1× beim Boot
        async def maintain_loop():
            import asyncio
            # Boot-Run mit kleinem Delay damit Server vorher steht
            await asyncio.sleep(15)
            try:
                log.info("maintain: boot run")
                _run_maintain_locked()
            except Exception as e:  # noqa: BLE001
                log.error("maintain boot run failed: %s", e)
            # Periodic-Loop
            while True:
                try:
                    await asyncio.sleep(600)  # 10 Min
                    log.info("maintain: periodic run")
                    _run_maintain_locked()
                except asyncio.CancelledError:
                    break
                except Exception as e:  # noqa: BLE001
                    log.error("maintain periodic run failed: %s", e)

        import asyncio
        task = asyncio.create_task(maintain_loop())
        try:
            async with mcp_app.router.lifespan_context(app):
                yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            audit.log_event("server_stop")

    # Routes: health + OAuth-Endpoints (public, vor Auth-Middleware) + MCP (auth-protected)
    routes_list = [
        Route("/health", endpoint=health, methods=["GET"]),
        *oauth_routes.routes(),  # .well-known/* + /oauth/*
        # Beide Pfade ohne 307-Redirect (claude.ai BETA folgt nicht).
        Mount("/mcp", app=mcp_app),
        Mount("/mcp/", app=mcp_app),
    ]

    app = Starlette(
        debug=False,
        middleware=[
            Middleware(RateLimitMiddleware),   # OUTER: erst Rate-Limit ...
            Middleware(DualAuthMiddleware),    # ... dann Auth (JWT oder Bearer)
        ],
        routes=routes_list,
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False
    return app


def _boot_security_check() -> None:
    """Startup-Validation: warnt bei schwacher Auth-Konfiguration.

    Logs warnings statt zu crashen — Server startet trotzdem (z.B. Dev-Setup),
    aber Operator sieht klar wenn was schwach ist.
    """
    # Bearer-Token-Length
    if MCP_TOKEN and len(MCP_TOKEN) < 32:
        log.warning(
            "  ⚠ MCP_TOKEN nur %d chars (Min. empfohlen: 32). "
            "Generiere via: python -c \"import secrets; print(secrets.token_urlsafe(32))\"",
            len(MCP_TOKEN),
        )

    # Audit-Log-Pfad schreibbar?
    audit_path = os.environ.get("MCP_AUDIT_LOG", "/var/log/mcp/audit.log")
    audit_dir = os.path.dirname(audit_path) or "."
    try:
        os.makedirs(audit_dir, exist_ok=True)
        # Test-write
        test_file = os.path.join(audit_dir, ".write_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.unlink(test_file)
    except (OSError, PermissionError) as e:
        log.warning("  ⚠ Audit-Log-Verzeichnis %s NICHT schreibbar (%s). "
                    "Audit-Events gehen verloren!", audit_dir, e)

    # OAuth-Strength
    if oauth.is_configured():
        ok_jwt, msg_jwt = oauth.is_jwt_secret_strong()
        if not ok_jwt:
            log.warning("  ⚠ JWT-Secret: %s", msg_jwt)
        ok_pw, msg_pw = oauth.is_password_hash_strong()
        if not ok_pw:
            log.warning("  ⚠ Password-Hash: %s", msg_pw)
        else:
            log.info("  Password-Hash: %s", msg_pw)


def main() -> None:
    log.info("KI-OS MCP Server starting")
    log.info("  Vault: %s (exists=%s)", vault.VAULT_PATH, vault.VAULT_PATH.exists())
    log.info("  Bind:  %s:%d", MCP_HOST, MCP_PORT)
    log.info("  Auth:  Bearer=%s, OAuth=%s",
             "enabled" if MCP_TOKEN else "DISABLED",
             "enabled" if oauth.is_configured() else "DISABLED")
    if oauth.is_configured():
        log.info("  OAuth: issuer=%s, user=%s, db=%s",
                 oauth.OAUTH_ISSUER, oauth.OAUTH_USER_EMAIL, oauth.OAUTH_DB_PATH)
    _boot_security_check()
    uvicorn.run(create_app(), host=MCP_HOST, port=MCP_PORT, log_level="info")


if __name__ == "__main__":
    main()
