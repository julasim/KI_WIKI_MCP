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


def delete_file(rel: str) -> None:
    """Löscht eine Datei aus dem Vault. Räumt leere Parent-Folder auf."""
    p = safe_path(rel)
    if not p.is_file():
        raise VaultError(f"Datei nicht gefunden: {rel}")
    p.unlink()
    # Leere Eltern-Folder aufräumen (bis VAULT_PATH, exklusiv)
    parent = p.parent
    while parent != VAULT_PATH and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


# ---------- Task-Finder -------------------------------------------------------


def find_task(task_id_or_slug: str) -> Path | None:
    """Findet einen Task per ID (`t-foo`) oder slug (`foo`).

    Tasks liegen in 10_Life/tasks/<slug>.md. Filename ist slug ohne `t-` Prefix.
    """
    slug = task_id_or_slug.removeprefix("t-")
    candidate = VAULT_PATH / "10_Life" / "tasks" / f"{slug}.md"
    if candidate.is_file():
        return candidate
    return None


# ---------- Wikilinks ---------------------------------------------------------

# [[id]], [[id|display]], [[id#anchor]], [[id#anchor|display]]
# Capture: 1=id, 2=anchor (mit #), 3=display (mit |)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(#[^\]|]*)?(\|[^\]]*)?\]\]")


def find_wikilink_refs(target_id: str) -> list[tuple[Path, list[int]]]:
    """Findet alle .md-Files die [[target_id]] enthalten (egal ob mit
    display-text oder anchor). Returns Liste von (path, [line_nums]).
    """
    target = target_id.strip()
    out: list[tuple[Path, list[int]]] = []
    for path in walk_md():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        line_nums: list[int] = []
        for i, line in enumerate(text.split("\n"), 1):
            for m in _WIKILINK_RE.finditer(line):
                if m.group(1).strip() == target:
                    line_nums.append(i)
                    break
        if line_nums:
            out.append((path, line_nums))
    return out


# ---------- Auto-Notes Block in Project-READMEs ------------------------------

# Markers für auto-generierten Notes-Block in Projekt-READMEs.
# Zwischen den Markern wird vom Server gepflegt; alles drumrum ist manueller
# Content und bleibt unangetastet.
AUTO_NOTES_START = "<!-- AUTO-NOTES-START -->"
AUTO_NOTES_END = "<!-- AUTO-NOTES-END -->"


def update_auto_notes_block(readme_path: Path, sections: dict[str, list[dict[str, str]]]) -> bool:
    """Updated den Auto-Notes-Block in einem README.

    Args:
        readme_path: absoluter Pfad zur README.md
        sections: dict {section_label: [{"id": "...", "title": "...", "date": "..."}]}
                  z.B. {"Notes": [...], "Meetings": [...]}

    Returns:
        True wenn das File geändert wurde.

    Erstellt Block falls nicht vorhanden (am Ende des Files).
    """
    if not readme_path.is_file():
        return False
    text = readme_path.read_text(encoding="utf-8").replace("\r\n", "\n")

    # Auto-Block bauen
    lines = [AUTO_NOTES_START, ""]
    has_content = False
    for label, items in sections.items():
        if not items:
            continue
        has_content = True
        lines.append(f"## {label}")
        lines.append("")
        # Sortieren: nach Datum absteigend
        items_sorted = sorted(items, key=lambda x: x.get("date", ""), reverse=True)
        for item in items_sorted:
            date = item.get("date", "")
            date_prefix = f"`{date}` " if date else ""
            lines.append(f"- {date_prefix}[[{item['id']}|{item['title']}]]")
        lines.append("")
    lines.append(AUTO_NOTES_END)

    if not has_content:
        # Leerer Block: trotzdem mit Markern (idempotent für nächsten Run)
        new_block = f"{AUTO_NOTES_START}\n{AUTO_NOTES_END}"
    else:
        new_block = "\n".join(lines)

    if AUTO_NOTES_START in text and AUTO_NOTES_END in text:
        # Existing block ersetzen — robust gegen mehrfache Marker im File:
        # split nur am ersten/letzten Marker, nicht an allen.
        before = text.split(AUTO_NOTES_START, 1)[0].rstrip()
        after = text.rsplit(AUTO_NOTES_END, 1)[1].lstrip()
        new_text = before + "\n\n" + new_block + ("\n\n" + after if after else "\n")
    else:
        # Append am Ende
        new_text = text.rstrip() + "\n\n" + new_block + "\n"

    if new_text == text:
        return False
    readme_path.write_text(new_text, encoding="utf-8")
    return True


def collect_project_content(project_slug: str) -> dict[str, list[dict[str, str]]]:
    """Sammelt alle Notes + Meetings unter 05_Projects/<slug>/.

    Returns: {"Notes": [...], "Meetings": [...]}
    """
    project_dir = VAULT_PATH / "05_Projects" / project_slug
    sections: dict[str, list[dict[str, str]]] = {"Notes": [], "Meetings": []}
    if not project_dir.is_dir():
        return sections

    # Notes-Subfolder
    for sub in ("notes", "meetings"):
        sub_dir = project_dir / sub
        if not sub_dir.is_dir():
            continue
        label = "Notes" if sub == "notes" else "Meetings"
        for path in sorted(sub_dir.glob("*.md")):
            try:
                post = read_post(rel_path(path))
            except Exception:  # noqa: BLE001
                continue
            fm = post.metadata
            if not fm.get("id"):
                continue
            sections[label].append({
                "id": str(fm["id"]),
                "title": str(fm.get("title", path.stem)),
                "date": str(fm.get("date", fm.get("created", ""))),
            })

    # Top-level Files (z.B. dashboard.md, stundenaufzeichnung.md)
    top_level = []
    for path in sorted(project_dir.glob("*.md")):
        if path.name.lower() in ("readme.md", "context.md"):
            continue
        try:
            post = read_post(rel_path(path))
        except Exception:  # noqa: BLE001
            continue
        fm = post.metadata
        if not fm.get("id"):
            continue
        ftype = fm.get("type", "note")
        if ftype in ("note", "meeting"):
            top_level.append({
                "id": str(fm["id"]),
                "title": str(fm.get("title", path.stem)),
                "date": str(fm.get("date", fm.get("created", ""))),
            })
    if top_level:
        sections.setdefault("Sonstige", []).extend(top_level)

    return sections


def find_related_refs(target_id: str) -> list[Path]:
    """Findet alle .md-Files die `target_id` in ihrer Frontmatter
    `related[]`-Liste haben (egal ob auch im Body verlinkt oder nicht).
    """
    target = target_id.strip()
    out: list[Path] = []
    for path in walk_md():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            post = frontmatter.loads(text)
        except Exception:  # noqa: BLE001
            continue
        related = post.metadata.get("related")
        if isinstance(related, list) and target in related:
            out.append(path)
    return out


def replace_wikilinks(text: str, old_id: str, new_id: str) -> tuple[str, int]:
    """Ersetzt alle [[old_id]] / [[old_id|display]] / [[old_id#anchor]] etc.
    durch new_id, behält display + anchor bei. Returns (new_text, count).
    """
    old = old_id.strip()
    count = 0

    def sub(m: re.Match[str]) -> str:
        nonlocal count
        if m.group(1).strip() != old:
            return m.group(0)
        count += 1
        anchor = m.group(2) or ""
        display = m.group(3) or ""
        return f"[[{new_id}{anchor}{display}]]"

    return _WIKILINK_RE.sub(sub, text), count


def migrate_id_in_related(post: frontmatter.Post, old_id: str, new_id: str) -> bool:
    """Ersetzt old_id durch new_id in frontmatter `related` Liste.
    Returns True wenn was geändert wurde."""
    related = post.metadata.get("related")
    if not isinstance(related, list):
        return False
    new_list = [new_id if r == old_id else r for r in related]
    if new_list == related:
        return False
    post["related"] = new_list
    return True


# ---------- Slug-Constraint ---------------------------------------------------

SLUG_MAX = 60


def validate_slug(slug: str) -> str | None:
    """Returns Fehlertext wenn ungültig, sonst None."""
    if not slug:
        return "Leerer slug"
    if len(slug) > SLUG_MAX:
        return f"Slug zu lang ({len(slug)} > {SLUG_MAX} Zeichen)"
    if not re.fullmatch(r"[a-z0-9äöüß\-]+", slug):
        return f"Slug enthält ungültige Zeichen: {slug!r}"
    return None


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


# ---------- 5y-Goal-Pfad-Konstanten ------------------------------------------
# Single Source of Truth für alle Reader die unter 10_Life/goals/5y-2031/
# liegen (vision, säulen, drift, tracker/habits etc.).

GOAL_SLUG = "5y-2031"


def goal_base() -> Path:
    """Absoluter Pfad zum 5y-Goal-Folder (10_Life/goals/5y-2031/)."""
    return VAULT_PATH / "10_Life" / "goals" / GOAL_SLUG


def goal_file(*parts: str) -> Path:
    """Pfad innerhalb des 5y-Goal-Folders."""
    return goal_base().joinpath(*parts)
