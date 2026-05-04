"""Vault-Helper — alle Pfad- + Frontmatter-Operationen.

Das Vault ist Single Source of Truth (Markdown + YAML-Frontmatter).
Alle Tools gehen durch diese Helfer, damit Pfad-Validierung +
Frontmatter-Konventionen (id, type, status, created, updated, tags)
zentral bleiben.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter
import yaml

VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/vault")).resolve()


# ---------- Pfad-Sicherheit ----------------------------------------------------


class VaultError(Exception):
    """Fehler bei Vault-Operationen (Pfad ausserhalb, Datei fehlt, ...)."""


def safe_path(rel: str) -> Path:
    """Resolve `rel` gegen VAULT_PATH und stelle sicher dass das Ziel
    INNERHALB des Vault liegt (kein `..`-Escape).

    Akzeptiert sowohl `10_Life/daily/2026-05-03.md` als auch
    `/10_Life/daily/2026-05-03.md`.
    """
    rel = rel.lstrip("/\\")
    target = (VAULT_PATH / rel).resolve()
    try:
        target.relative_to(VAULT_PATH)
    except ValueError as e:
        raise VaultError(f"Pfad ausserhalb des Vault: {rel}") from e
    return target


def rel_path(p: Path) -> str:
    """Rel-Pfad ab VAULT_PATH, mit Forward-Slashes."""
    return str(p.relative_to(VAULT_PATH)).replace("\\", "/")


# ---------- Slug + IDs --------------------------------------------------------

_SLUG_KEEP = re.compile(r"[^a-z0-9äöüß\-]+")


def slugify(text: str) -> str:
    """Slug-Regel (übernommen aus Bot): kleinbuchstaben, Umlaute erhalten,
    Spaces → `-`, sonst alles raus."""
    s = text.strip().lower()
    # NFC normalisieren damit ä/ö/ü als single codepoint bleiben
    s = unicodedata.normalize("NFC", s)
    s = s.replace(" ", "-")
    s = _SLUG_KEEP.sub("", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "untitled"


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- File-Read ---------------------------------------------------------


def read_text(rel: str) -> str:
    """Liest UTF-8 + normalisiert CRLF → LF (wichtig auf Windows-mounted vaults)."""
    p = safe_path(rel)
    if not p.is_file():
        raise VaultError(f"Datei nicht gefunden: {rel}")
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def read_post(rel: str) -> frontmatter.Post:
    """Liest Markdown + parst Frontmatter."""
    raw = read_text(rel)
    return frontmatter.loads(raw)


def write_post(rel: str, post: frontmatter.Post) -> None:
    """Schreibt Frontmatter+Body, setzt updated."""
    post["updated"] = today_iso()
    p = safe_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")


# ---------- Listing -----------------------------------------------------------


def list_dir(rel: str) -> list[dict[str, Any]]:
    """Listet Inhalt eines Folders (Files + Subfolders, sortiert)."""
    if rel in ("", "/"):
        d = VAULT_PATH
    else:
        d = safe_path(rel)
    if not d.is_dir():
        raise VaultError(f"Folder nicht gefunden: {rel}")

    entries: list[dict[str, Any]] = []
    for child in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        if child.is_dir():
            entries.append(
                {"kind": "dir", "name": child.name, "path": rel_path(child)}
            )
        else:
            stat = child.stat()
            entries.append(
                {
                    "kind": "file",
                    "name": child.name,
                    "path": rel_path(child),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                }
            )
    return entries


def walk_md(rel: str = "") -> list[Path]:
    """Alle .md-Files unter `rel` (rekursiv)."""
    base = VAULT_PATH if rel in ("", "/") else safe_path(rel)
    if not base.is_dir():
        raise VaultError(f"Folder nicht gefunden: {rel}")
    out: list[Path] = []
    for p in base.rglob("*.md"):
        # Skip hidden + archive
        parts = p.relative_to(VAULT_PATH).parts
        if any(part.startswith(".") for part in parts):
            continue
        out.append(p)
    return out


# ---------- Search ------------------------------------------------------------


def grep_vault(
    query: str,
    *,
    scope: str = "",
    case_sensitive: bool = False,
    max_results: int = 50,
    context: int = 1,
) -> list[dict[str, Any]]:
    """Volltext-Suche durch alle .md (regex). Liefert Treffer mit Kontext."""
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(query, flags)
    except re.error as e:
        raise VaultError(f"Ungültiges Regex-Pattern: {e}") from e

    hits: list[dict[str, Any]] = []
    for path in walk_md(scope):
        try:
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError:
            continue
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if pattern.search(line):
                start = max(0, i - context)
                end = min(len(lines), i + context + 1)
                snippet = "\n".join(lines[start:end])
                hits.append(
                    {
                        "path": rel_path(path),
                        "line": i + 1,
                        "match": line.strip()[:200],
                        "context": snippet,
                    }
                )
                if len(hits) >= max_results:
                    return hits
    return hits


# ---------- Convenience: Daily ------------------------------------------------


def daily_path(d: str | None = None) -> str:
    """Rel-Pfad zur Daily-Note für ISO-Datum (default heute)."""
    iso = d or today_iso()
    return f"10_Life/daily/{iso}.md"


def append_to_daily(
    text: str, *, section: str = "Notizen & Gedanken", d: str | None = None
) -> str:
    """Hängt `text` unter Section `## {section}` an die Daily-Note an.
    Erstellt die Daily-Note (mit Skeleton) wenn sie noch nicht existiert.
    Returns rel-pfad."""
    rel = daily_path(d)
    p = safe_path(rel)
    if not p.exists():
        _create_daily_skeleton(rel)
    raw = read_text(rel)
    needle = f"## {section}"
    if needle in raw:
        # Nach Section-Header die nächste leere Zeile finden, dann anhängen
        parts = raw.split(needle, 1)
        before = parts[0] + needle
        rest = parts[1]
        # Append direkt nach Header (auf neuer Zeile)
        new = before + "\n" + text.rstrip() + rest
    else:
        new = raw.rstrip() + f"\n\n{needle}\n{text.rstrip()}\n"
    p.write_text(new, encoding="utf-8")
    return rel


def _create_daily_skeleton(rel: str) -> None:
    iso = Path(rel).stem  # "2026-05-03"
    fm = {
        "id": f"daily-{iso}",
        "type": "daily",
        "title": iso,
        "date": iso,
        "created": iso,
        "updated": iso,
        "status": "active",
        "tags": ["daily"],
        "energy": None,
        "mood": None,
        "key_insight": None,
    }
    body = (
        f"\n# {iso}\n---\n"
        "## Heute\n\n"
        "## Notizen & Gedanken\n\n"
        "## Offen / Einsortieren\n- [ ] \n\n"
        "## Abends\n- Was lief gut?\n- Was nehme ich mit?\n\n"
        "---\n→ [Life-Index](../_index.md) · [MOC](../../MOC.md)\n"
    )
    p = safe_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=True) + "---\n" + body, encoding="utf-8")
