#!/usr/bin/env python3
"""Token-Rotation für KI-OS MCP Server.

Generiert einen neuen MCP_TOKEN, schiebt den alten auf MCP_TOKEN_LEGACY.
Nach Restart akzeptiert der Server beide Tokens für die Übergangszeit.
Stelle dann alle Clients auf den neuen Token um, dann löschst du
MCP_TOKEN_LEGACY und restartest erneut.

Verwendung am VPS:
  cd /opt/mcp
  python3 scripts/rotate_token.py [--env-file .env]

Output: alter Token als legacy gespeichert, neuer Token als MCP_TOKEN.
Restart-Hinweis am Ende.
"""

import argparse
import os
import re
import secrets
import sys
from pathlib import Path


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # Strip optionale Quotes
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out[key] = val
    return out


def write_env(path: Path, current: dict[str, str], updates: dict[str, str | None]) -> None:
    """Update existing keys in-place, append new ones."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    out_lines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
        if m and m.group(1) in updates:
            key = m.group(1)
            new_val = updates[key]
            seen.add(key)
            if new_val is None:
                # Skip = remove
                continue
            out_lines.append(f"{key}={new_val}")
        else:
            out_lines.append(line)
    # Append new keys
    for key, val in updates.items():
        if key in seen or val is None:
            continue
        out_lines.append(f"{key}={val}")
    path.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP Token-Rotation")
    parser.add_argument("--env-file", default=".env", help="Pfad zur .env-Datei")
    parser.add_argument("--clear-legacy", action="store_true",
                        help="Nur MCP_TOKEN_LEGACY entfernen (nach Migration aller Clients)")
    args = parser.parse_args()

    env_path = Path(args.env_file).resolve()
    if not env_path.exists():
        print(f"❌ {env_path} nicht gefunden", file=sys.stderr)
        return 1

    env = parse_env(env_path)
    current = env.get("MCP_TOKEN", "")

    if args.clear_legacy:
        write_env(env_path, env, {"MCP_TOKEN_LEGACY": None})
        print(f"✓ MCP_TOKEN_LEGACY entfernt aus {env_path}")
        print()
        print("Restart Container damit Legacy-Token nicht mehr akzeptiert wird:")
        print("  cd /opt/ki-os && docker compose up -d --force-recreate mcp")
        return 0

    if not current:
        print(f"❌ MCP_TOKEN ist leer in {env_path} — nichts zum rotieren", file=sys.stderr)
        return 1

    new_token = secrets.token_urlsafe(32)
    write_env(env_path, env, {
        "MCP_TOKEN": new_token,
        "MCP_TOKEN_LEGACY": current,
    })

    print("=" * 60)
    print("✓ TOKEN ROTATED")
    print("=" * 60)
    print()
    print(f"Alt (jetzt LEGACY, akzeptiert bis manuell entfernt):")
    print(f"  {current[:8]}...{current[-6:]}")
    print()
    print(f"Neu (MCP_TOKEN):")
    print(f"  {new_token}")
    print()
    print("Nächste Schritte:")
    print(f"  1. Container restarten:")
    print(f"     cd /opt/ki-os && docker compose up -d --force-recreate mcp")
    print(f"  2. Client-Configs updaten auf NEUEN Token")
    print(f"  3. Wenn alle Clients migriert: Legacy entfernen via:")
    print(f"     python3 {Path(__file__).name} --clear-legacy")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
