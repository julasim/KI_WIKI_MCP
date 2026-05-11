# KI_WIKI_MCP — MCP-Server (Vault-Backend)

Python-FastMCP-Server. 58 Tools, alle mit Audit-Wrapper + Snapshot-Protection für destruktive Ops. Dient Bot + Dashboard + externen Clients (Claude Desktop, claude.ai-Connectors).

## Schlüssel-Dateien

| Datei/Pfad | Was |
|---|---|
| `ki_os_mcp/server.py` | FastMCP-Bootstrap, Auth (Bearer + OAuth), Allowed-Hosts, Tool-Registry |
| `ki_os_mcp/vault.py` | Filesystem-Layer, UTF-8 enforce, CRLF-Normalisierung, `safe_path()` |
| `ki_os_mcp/audit.py` | JSONL-Audit-Log aller Tool-Calls + Auth-Events |
| `scripts/rotate_token.py` | Token-Rotation mit Legacy-Übergang |
| `scripts/set_oauth_password.py` | bcrypt-Hash für OAuth-User |

## Auth-Modell

Dual-Auth — beides kann parallel aktiv sein:

1. **Bearer-Token** (`MCP_TOKEN` in env) — Service-zu-Service, Bot/Dashboard nutzen das
2. **OAuth 2.1** mit JWT (`OAUTH_*` env vars) — externe Clients (Claude.ai-Connectors etc.)

OAuth-State (Refresh-Tokens, registered Clients) in SQLite unter `/var/lib/mcp-oauth/oauth.db` (mounted nach `/opt/mcp-oauth/`).

### Token-Rotation
```bash
cd /opt/KI_WIKI_MCP && python3 scripts/rotate_token.py
# Output zeigt neuen Token + nennt Schritte:
# 1. MCP recreaten
# 2. Bot + Dashboard .env auf neuen Token bringen + recreaten
# 3. Wenn alles läuft: python3 scripts/rotate_token.py --clear-legacy
```
Legacy-Token bleibt nach Rotation parallel gültig — Bot/Dashboard können in Ruhe migriert werden.

## DNS-Rebinding-Protection (allowed_hosts)

MCP-SDK enforced Host-Header-Whitelist (`enable_dns_rebinding_protection=True`). `_DEFAULT_ALLOWED_HOSTS` in `server.py` enthält:
- Public-Domain (`wiki-mcp.sima.business`)
- sslip.io-Fallback
- `localhost`, `127.0.0.1` (mit/ohne `:5002`)
- **`ki-os-mcp` + `ki-os-mcp:5002`** — Container-Hostname, damit Bot/Dashboard intern POSTen können

Override via `MCP_ALLOWED_HOSTS` env (comma-separated) — **ersetzt** die Defaults, ergänzt nicht. Wenn override, IMMER die Container-Hostnames mit reinnehmen, sonst kommt 421 von POSTs aus dem Stack.

## Vault-Layout-Annahmen

Vault unter `/vault` im Container, gemounted von `/opt/vault/KI_WIKI_Vault/` auf VPS. Erwartete Top-Level-Struktur:
```
01_Raw/                 Inbox, articles
06_Meta/                Bot-Memory, Reminders, etc.
10_Life/                Daily, Sport, Habits, Books, Wins
20_*, 30_*              Projekte, Themen
09_Attachments/         Bot-Uploads
```
`safe_path()` in `vault.py` verhindert Traversal über VAULT-Root hinaus.

## File-IO-Konventionen

- **Alle Reads/Writes mit `encoding="utf-8"`** — keine Locale-Defaults. Container-Locale ist Container-default (typisch C.UTF-8 oder gar nichts), Python-Code setzt explizit utf-8
- **CRLF→LF beim Read** (`vault.read_text`) — Windows-OneDrive-Files normalisieren
- **`atomic_write`-Pattern** für alle Schreibops (temp-file + rename) — kein partieller Write bei Crash
- **Snapshots** vor destruktiven Ops (`move`, `delete`, `edit_file`) als tar.gz nach `/snapshots/` (host: `/opt/mcp-snapshots/`)

## Audit-Log

Jeder Tool-Call schreibt JSONL nach `/var/log/mcp/audit.log` (host: `/opt/mcp-logs/`). Format: `{ts, user, tool, args, ok, duration_ms, ...}`. **JSON-Serialization mit `ensure_ascii=False`** — Umlaute bleiben als `ä` statt `ä`. Wenn das Konsistenz-Probleme macht, im Audit-Wrapper anpassen, nicht im Tool-Result-Pfad.

## Container

- Im Stack als `ki-os-mcp`, auf `default` + `proxy` networks
- `TZ=Europe/Vienna` ist gesetzt (kritisch für Recurring-Task-Logik — sonst springt `today` nach 22:00 Vienna schon in den nächsten UTC-Tag)
- Healthcheck: `GET /health` → 200 (siehe `docker-compose.yml`)
- Externe Verbindungen ausschließlich via Edge-Caddy (Reverse-Proxy in `/opt/Proxy/`)

## Edge-Proxy / Public-Reachability

Der MCP wird vom **edge-caddy** (Repo `julasim/Proxy`, deployed `/opt/Proxy/`) auf `wiki-mcp.sima.business` und Fallback `76-13-10-79.sslip.io` exponiert. Caddyfile-Block:

```caddyfile
wiki-mcp.sima.business {
    reverse_proxy ki-os-mcp:5002 { flush_interval -1; header_up X-Real-IP {remote_host} }
}
```

Der `ki-os-mcp`-Container hängt am externen Docker-Netz `proxy` (= das vom edge-caddy verwaltete Netz, geteilt mit anderen App-Stacks). Mehr zur Multi-Stack-Architektur in `/opt/Proxy/CLAUDE.md` oder im `Proxy`-Repo selbst.

**Goldene Regeln (Verstoß = Stack-übergreifender 80/443-Konflikt):**
- Kein eigener Caddy/Nginx mit `ports: "80:80"` im Compose
- Container-Name `ki-os-mcp` nicht ändern — edge-caddy's Caddyfile referenziert per Name
- Bei `MCP_ALLOWED_HOSTS`-Override: IMMER `ki-os-mcp` + `ki-os-mcp:5002` mit reinnehmen (sonst 421 von POSTs aus Bot/Dashboard)
- Public-URL nur für externe Clients (Claude Desktop) — Bot/Dashboard erreichen MCP intern via `http://ki-os-mcp:5002/mcp/`

## Häufige Fallen

- **421 Misdirected Request von POSTs** → MCP_ALLOWED_HOSTS Override hat Container-Hostnames vergessen
- **OAuth-DB leer nach Recreate** → `mcp-oauth` Volume-Mount in `docker-compose.yml` prüfen, nicht löschen
- **bcrypt-Hashes mit `$`** in `.env` brauchen `$$`-Escape wegen docker-compose env_file Parser
- **`docker exec ki-os-mcp file ...`** funktioniert nicht — `file`-Binary fehlt im Slim-Image. `python -c "..."` nutzen für Filetype-Checks

## Externe Clients

Public-URL für Claude-Connectors:
- MCP-Endpoint: `https://wiki-mcp.sima.business/mcp/`
- OAuth-Discovery: `https://wiki-mcp.sima.business/.well-known/oauth-authorization-server`
- Issuer/Resource konfiguriert via `OAUTH_ISSUER` / `OAUTH_RESOURCE` env
