"""Setup-Script: OAuth-Password setzen.

Fragt das Passwort interaktiv ab (verlaesst nie die Console),
generiert bcrypt-Hash und gibt eine Zeile aus die du in /opt/mcp/.env
einfuegst.

Usage:
    docker exec -it ki-os-mcp python /app/scripts/set_oauth_password.py

WICHTIG: '-it' damit getpass funktioniert (interaktive TTY).
"""

from __future__ import annotations

import getpass
import os
import sys

import bcrypt


def main() -> int:
    print("KI-OS OAuth — Password setzen")
    print("-" * 40)
    email = os.environ.get("OAUTH_USER_EMAIL", "julius@sima.or.at")
    print(f"User: {email}")
    print()

    pw1 = getpass.getpass("Neues Passwort: ")
    if not pw1 or len(pw1) < 8:
        print("FAIL: Passwort muss >= 8 Zeichen sein.")
        return 1
    pw2 = getpass.getpass("Wiederholen: ")
    if pw1 != pw2:
        print("FAIL: Passwoerter stimmen nicht ueberein.")
        return 1

    h = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt(rounds=12))
    hash_str = h.decode("ascii")

    print()
    print("OK — fuege diese Zeile in /opt/mcp/.env ein:")
    print()
    print(f"OAUTH_PASSWORD_HASH={hash_str}")
    print()
    # Plus JWT-Secret falls noch nicht da
    if not os.environ.get("OAUTH_JWT_SECRET"):
        import secrets
        jwt_secret = secrets.token_urlsafe(48)
        print("Plus (falls noch nicht in .env):")
        print(f"OAUTH_JWT_SECRET={jwt_secret}")
        print(f"OAUTH_USER_EMAIL={email}")
        print()
    print("Danach: docker compose -f /opt/ki-os/docker-compose.yml restart mcp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
