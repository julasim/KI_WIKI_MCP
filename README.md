# KI-OS MCP Server

**Mittelring der KI-OS-Architektur** — stellt das Vault als
[Model Context Protocol](https://modelcontextprotocol.io/) Endpoint bereit,
damit Claude Code, claude.ai, Cursor und andere MCP-Clients direkt mit
dem Vault arbeiten können.

```
Client (Claude Code / claude.ai / Cursor / ...)
        │
        ▼ HTTPS + Bearer Token
   Caddy :443                      ← TLS terminieren (Let's Encrypt)
        │
        ▼ http (intern, docker-Netzwerk)
   ki-os-mcp :3002                 ← MCP Streamable HTTP
        │
        ▼ Filesystem
   /opt/vault/KI_WIKI_Vault        ← Markdown + YAML Frontmatter
```

## Was kann er

**14 Tools, gruppiert nach Funktion:**

### Lesen / Suchen
| Tool | Zweck |
|---|---|
| `search_vault` | Volltext-Regex über alle `.md` (mit Scope, Context-Lines) |
| `read_file` | Markdown + parsed Frontmatter, oder raw bei Nicht-MD |
| `list_files` | Folder-Inhalt (Files + Subfolders, mtime, Größe) |

### Schreiben (Capture)
| Tool | Zweck |
|---|---|
| `create_note` | Note in `10_Life/notes/` oder `05_Projects/<slug>/notes/` |
| `create_task` | Task in `10_Life/tasks/` mit optional `recurrence` |
| `create_meeting` | Meeting in `10_Life/meetings/` oder `05_Projects/<slug>/meetings/` |
| `append_to_daily` | An eine Section der Daily-Note anhängen (auto-create wenn fehlt) |
| `goal_log` | Append zu `10_Life/goals/<goal>/<subtype>.md` |
| `project_context` | Update `05_Projects/<slug>/CONTEXT.md` |

### Editieren / Löschen
| Tool | Zweck |
|---|---|
| `edit_file` | Frontmatter dict-merge (None=Feld löschen) + Body ersetzen |
| `task` | Status-Actions: `done` / `reopen` / `snooze` / `edit` |
| `move` | File rename + **automatische Wikilink-Migration** im ganzen Vault |
| `request_delete` | Step 1 of Two-Step-Delete: Preview + Confirm-Token |
| `confirm_delete` | Step 2: tatsächliche Löschung via Token (in-memory pending list) |

## Schemakonform & sicher

- **SCHEMA.md**-konforme Pfade: Tasks immer in `10_Life/tasks/`, Notes in
  `10_Life/notes/YYYY-MM-DD_<slug>.md`, Meetings analog
- **Slug-Validation**: max 60 Zeichen, nur `[a-z0-9äöüß-]`
- **Pfad-Sicherheit**: Alle Pfade gegen `VAULT_PATH` validiert,
  kein `..`-Escape möglich
- **CRLF-Normalisierung**: Windows-mounted Vaults funktionieren
- **Bearer-Auth**: `Authorization: Bearer <MCP_TOKEN>` Pflicht
- **Two-Step-Delete**: nichts wird ohne explizite Bestätigung gelöscht
- **Move-Dry-Run**: Wikilink-Migration kann erst geprüft werden bevor
  geschrieben wird

## Lokal entwickeln

```bash
git clone https://github.com/julasim/KI_WIKI_MCP.git
cd KI_WIKI_MCP

python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e .

cp .env.example .env
# In .env: MCP_TOKEN setzen, VAULT_PATH auf lokales Vault zeigen
$env:VAULT_PATH = "Z:\vault\KI_WIKI_Vault"   # PowerShell
$env:MCP_TOKEN = "devtoken"
python -m ki_os_mcp.server
```

Healthcheck: `curl http://localhost:3002/health`

## VPS-Deploy (im KI-OS Stack)

Der Server läuft als 4. Container im
[`KI_WIKI_Stack`](https://github.com/julasim/KI_WIKI_Stack):

```bash
ssh root@VPS
cd /opt
git clone https://github.com/julasim/KI_WIKI_MCP.git mcp
cd /opt/mcp
cp .env.example .env
# MCP_TOKEN generieren + eintragen:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

cd /opt/ki-os && bash update.sh
```

Caddy holt automatisch ein Let's-Encrypt-Zertifikat für die Domain
(default `<vps-ip>.sslip.io`, anpassbar im `Caddyfile` des Stack-Repos).

## Client-Konfiguration

### Claude Code (CLI)

```bash
claude mcp add \
  --transport http \
  --scope user \
  ki-os-vault \
  https://<vps-ip>.sslip.io/mcp/ \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

Verifizieren: `claude mcp list` → sollte `✓ Connected` zeigen.

### claude.ai (Web / Desktop)

Settings → **Connectors** → **Add custom connector**
- **Name:** KI-OS Vault
- **URL:** `https://<vps-ip>.sslip.io/mcp/`  *(trailing slash!)*
- **Authentication:** API Key → Header-Typ "Bearer" (oder "Both Legacy")
- **Token:** dein `MCP_TOKEN`

### Cursor / Continue.dev / Cline

In der jeweiligen `mcp.json` / Konfig:
```json
{
  "mcpServers": {
    "ki-os-vault": {
      "url": "https://<vps-ip>.sslip.io/mcp/",
      "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
    }
  }
}
```

## Tool-Beispiele

```python
# Volltext-Suche
search_vault(query="matura|abitur", scope="01_Projects", max_results=10)

# Frontmatter patchen (None=Feld löschen)
edit_file(
  path="10_Life/daily/2026-05-04.md",
  frontmatter_updates={"energy": 8, "key_insight": "MCP-Server live"}
)

# Task done markieren
task(id="t-dachboden-saugen", action="done")

# Recurring Task anlegen
create_task(
  title="Wäsche waschen",
  recurrence="weekly",
  context="@home",
  priority="medium"
)

# Move mit Wikilink-Migration (Dry-Run zuerst!)
move(
  source="10_Life/notes/2026-04-01_alte-notiz.md",
  dest="10_Life/notes/2026-05-04_neuer-titel.md",
  dry_run=True
)
# → würde 3 Files updaten, 5 Wikilinks rewriten
move(source=..., dest=..., dry_run=False)  # echt ausführen

# Two-Step Delete
{token} = request_delete(path="10_Life/notes/...md", reason="Duplikat")
confirm_delete(token=token)
```

## Architektur

- **Transport:** [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
  (offizieller MCP-Standard, ersetzt SSE)
- **SDK:** [`mcp`](https://github.com/modelcontextprotocol/python-sdk) (Anthropic Reference, Python)
- **Server-Framework:** Starlette + Uvicorn
- **Frontmatter:** `python-frontmatter` (YAML)
- **Single Source of Truth:** `/vault/` Bind-Mount → liest+schreibt
  direkt in den Markdown-Vault (gleiches Verzeichnis wie Bot+Dashboard)

## Sicherheit

- ✅ Bearer-Token-Auth via `MCP_TOKEN` env (Pflicht in Production)
- ✅ Pfad-Validierung gegen `VAULT_PATH` (kein `..`-Escape)
- ✅ Two-Step-Delete (nichts ohne Confirm)
- ✅ TLS via Caddy + Let's Encrypt (im Stack-Setup)
- ⚠ DNS-Rebinding-Protection ist deaktiviert (Bearer-Auth deckt das ab)
- ⚠ Kein Rate-Limiting (Phase 3 wenn nötig)
- ⚠ Kein Audit-Log (Phase 3)

## Status

- ✅ **Phase 1** (6 Tools): search/read/list + create_note/create_task/append_to_daily
- ✅ **Phase 2** (8 Tools): edit_file, task, create_meeting, request/confirm_delete, move,
  goal_log, project_context, plus Schema-Hardening
- ⏳ **Phase 3 (geplant):** edit_file mit Diff-Preview, Audit-Log, Rate-Limiting,
  OAuth-Auth optional
