# KI-OS MCP Server

Mittelring der KI-OS-Architektur: stellt das Vault als **Model Context Protocol (MCP)**
Endpoint bereit, damit Claude Code (Windows-PC) und andere MCP-Clients direkt
mit dem Vault arbeiten können.

## Architektur

```
Windows-PC (Claude Code)  ──HTTPS+Bearer──>  VPS:3002 (mcp-server)
                                                    │
                                                    └── /opt/vault/KI_WIKI_Vault
```

- **Transport:** Streamable HTTP (offizieller MCP-Standard seit März 2025)
- **Auth:** Bearer-Token via `Authorization`-Header
- **SDK:** [`mcp`](https://github.com/modelcontextprotocol/python-sdk) (Anthropic's Referenz)
- **Server-Framework:** Starlette + Uvicorn

## Tools (Phase 1)

| Tool              | Beschreibung                                   |
| ----------------- | ---------------------------------------------- |
| `search_vault`    | Volltext-Regex-Suche durch alle `.md`          |
| `read_file`       | Liest File + Frontmatter                       |
| `list_files`      | Folder-Inhalt (nicht rekursiv)                 |
| `create_note`     | Neue Note in Projekt-/Wiki-Folder              |
| `create_task`     | Neuer Task in `02_System/tasks/`               |
| `append_to_daily` | Hängt Text an Section der Daily-Note an        |

Phase 2 später: `edit_file`, `task(action='done|reopen|edit')`, `create_meeting`,
`move`, `goal_log`, `request_delete`, `confirm_delete`, `create_reminder`,
`project_context`.

## Lokal entwickeln

```bash
# Im mcp/-Repo
python -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -e .

# Lokal starten (gegen lokales Vault)
export VAULT_PATH="/path/to/KI_WIKI_Vault"
export MCP_TOKEN="devtoken"
python -m ki_os_mcp.server
```

Healthcheck: `curl http://localhost:3002/health`

## Deploy am VPS (via stack)

Im `ki-os-stack/docker-compose.yml` ist der Service `mcp` enthalten.

```bash
ssh root@VPS
cd /opt/bot && bash update.sh         # zieht alle Repos + rebuildet
docker compose -f /opt/ki-os/docker-compose.yml up -d mcp
```

Danach erreichbar unter `http://VPS:3002/mcp` (besser: hinter Reverse-Proxy mit TLS).

## Claude Code Konfiguration (Windows)

Edit `~/.claude.json` (oder Per-Project `.claude.json`):

```json
{
  "mcpServers": {
    "ki-os-vault": {
      "type": "http",
      "url": "https://mcp.deine-domain.tld/mcp",
      "headers": {
        "Authorization": "Bearer DEIN_MCP_TOKEN"
      }
    }
  }
}
```

Dann in Claude Code:

```
/mcp
```

Du solltest `ki-os-vault` mit 6 Tools sehen.

## Sicherheit

- **Immer** `MCP_TOKEN` setzen in Production. Generiere mit
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- **TLS via Reverse-Proxy** (Caddy/Traefik/nginx) — niemals Token unverschlüsselt!
- Server validiert alle Pfade gegen `VAULT_PATH` → kein `..`-Escape möglich
- Read-Operationen sind safe; Write-Operationen erzeugen neue Files (kein
  destructive overwrite ohne explizites edit-Tool, das kommt erst Phase 2)
