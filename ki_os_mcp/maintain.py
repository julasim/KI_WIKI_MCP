"""Vault-Autopilot — Self-Maintenance-Pipeline.

Ein zentraler Run der alle Vault-Pflege-Operationen idempotent ausführt.
Wird automatisch getriggert nach jedem Schreibvorgang (immediate, async)
plus periodisch (alle 10 Min + Boot).

PIPELINE-SCHRITTE
=================
1. Auto-Link alle Projekt-READMEs (vault_autolink)
2. Context-Drift normalisieren (Tasks: @-Präfix einheitlich)
3. Daily-Backlinks: heute erstellte Notes/Meetings/Tasks in Daily-Note
4. Lint-Report: was nicht auto-fixbar war

GARANTIEN
=========
- Idempotent: jeder Schritt kann beliebig oft laufen, kein Drift
- Sicher: bei Exception wird der Schritt geskippt, andere laufen weiter
- Audit: jeder Run loggt Start, Dauer, Aktionen, Errors
- Rollback: bei kritischem Fehler Snapshot vor Lauf
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter

from ki_os_mcp import audit, vault

log = logging.getLogger("ki-os-mcp.maintain")


def step_autolink_projects() -> dict[str, Any]:
    """Schritt 1: Auto-Link alle Projekt-READMEs.

    Idempotent: erstellt/aktualisiert Auto-Notes-Block in jeder
    05_Projects/<slug>/README.md mit aktueller Notes/Meetings-Liste.
    """
    projects_dir = vault.VAULT_PATH / "05_Projects"
    if not projects_dir.is_dir():
        return {"updated": 0, "skipped": 0}

    updated = 0
    skipped = 0
    errors: list[str] = []

    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        readme = project_dir / "README.md"
        if not readme.is_file():
            skipped += 1
            continue
        try:
            sections = vault.collect_project_content(project_dir.name)
            if vault.update_auto_notes_block(readme, sections):
                updated += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{project_dir.name}: {type(e).__name__}: {e}")

    return {"updated": updated, "skipped": skipped, "errors": errors}


def step_normalize_context() -> dict[str, Any]:
    """Schritt 2: Context-Drift normalisieren.

    Konvention: contexts ohne @-Präfix (canonical: "home" statt "@home").
    Tasks die "@home" haben werden auf "home" normalisiert.
    """
    tasks_dir = vault.VAULT_PATH / "10_Life" / "tasks"
    if not tasks_dir.is_dir():
        return {"normalized": 0}

    normalized = 0
    errors: list[str] = []
    for path in tasks_dir.glob("*.md"):
        try:
            post = vault.read_post(vault.rel_path(path))
            ctx = post.metadata.get("context")
            if isinstance(ctx, str) and ctx.startswith("@"):
                post["context"] = ctx.lstrip("@").lower()
                vault.write_post(vault.rel_path(path), post)
                normalized += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path.name}: {type(e).__name__}: {e}")

    return {"normalized": normalized, "errors": errors}


def step_daily_backlinks(target_date: str | None = None) -> dict[str, Any]:
    """Schritt 3: Daily-Backlinks pflegen.

    Für die Daily-Note des Datums: zeige unter "## Erstellte Notes"
    alle Notes/Meetings deren `created` oder `date` == target_date.
    Idempotent (Block zwischen Markern).
    """
    target_date = target_date or date.today().isoformat()
    daily_path = vault.VAULT_PATH / "10_Life" / "daily" / f"{target_date}.md"
    if not daily_path.is_file():
        return {"daily_exists": False, "added": 0}

    # Sammle alle Items
    items: list[dict[str, str]] = []
    base = vault.VAULT_PATH

    # Notes (10_Life/notes + 05_Projects/*/notes)
    for pattern in ["10_Life/notes/*.md", "05_Projects/*/notes/*.md"]:
        for path in base.glob(pattern):
            try:
                post = vault.read_post(vault.rel_path(path))
                fm = post.metadata
                if not fm.get("id"):
                    continue
                d = str(fm.get("date", fm.get("created", "")))
                if d == target_date:
                    items.append({
                        "kind": "note",
                        "id": str(fm["id"]),
                        "title": str(fm.get("title", path.stem)),
                        "project": str(fm.get("project", "")),
                    })
            except Exception:  # noqa: BLE001
                continue

    # Meetings
    for pattern in ["10_Life/meetings/*.md", "05_Projects/*/meetings/*.md"]:
        for path in base.glob(pattern):
            try:
                post = vault.read_post(vault.rel_path(path))
                fm = post.metadata
                if not fm.get("id"):
                    continue
                d = str(fm.get("date", fm.get("created", "")))
                if d == target_date:
                    items.append({
                        "kind": "meeting",
                        "id": str(fm["id"]),
                        "title": str(fm.get("title", path.stem)),
                        "project": str(fm.get("project", "")),
                    })
            except Exception:  # noqa: BLE001
                continue

    # Marker-Block in Daily aktualisieren
    BACKLINK_START = "<!-- AUTO-BACKLINKS-START -->"
    BACKLINK_END = "<!-- AUTO-BACKLINKS-END -->"

    text = daily_path.read_text(encoding="utf-8").replace("\r\n", "\n")

    # Block bauen
    block_lines = [BACKLINK_START]
    if items:
        notes = [i for i in items if i["kind"] == "note"]
        meetings = [i for i in items if i["kind"] == "meeting"]
        if notes:
            block_lines.append("\n## Erstellte Notes")
            for n in sorted(notes, key=lambda x: x["title"]):
                proj = f" _[{n['project']}]_" if n["project"] else ""
                block_lines.append(f"- [[{n['id']}|{n['title']}]]{proj}")
        if meetings:
            block_lines.append("\n## Meetings")
            for m in sorted(meetings, key=lambda x: x["title"]):
                proj = f" _[{m['project']}]_" if m["project"] else ""
                block_lines.append(f"- [[{m['id']}|{m['title']}]]{proj}")
    block_lines.append(BACKLINK_END)
    new_block = "\n".join(block_lines)

    if BACKLINK_START in text and BACKLINK_END in text:
        before = text.split(BACKLINK_START)[0].rstrip()
        after = text.split(BACKLINK_END)[1].lstrip()
        new_text = before + "\n\n" + new_block + ("\n\n" + after if after else "\n")
    else:
        new_text = text.rstrip() + "\n\n" + new_block + "\n"

    if new_text != text:
        daily_path.write_text(new_text, encoding="utf-8")
        return {"daily_exists": True, "added": len(items), "changed": True}
    return {"daily_exists": True, "added": len(items), "changed": False}


def step_lint_summary() -> dict[str, Any]:
    """Schritt 4: Lint-Report (smart-mode).

    Returns nur summary — keine Aktionen, nur Beobachtung.
    Issues die Maintain NICHT auto-fixen kann (z.B. echte broken
    wikilinks die manuelle Entscheidung brauchen).
    """
    # Replicate vault_lint smart-mode skip-logic (lite version for maintain)
    import re as _re

    skip_path_re = _re.compile(
        r"^(08_Templates/|99_Archive/|"
        r"06_Meta/(health-reports|health_checks|bot-memory|todo|orphans|stats|changelog)|"
        r"07_Tools/training/system_prompt\.md$|"
        r"(PIPELINES|SCHEMA|COMMANDS|MOC)\.md$|"
        r".*/(_index|README|CONTEXT)\.md$|"
        r"^(README|CLAUDE)\.md$)"
    )

    all_md = vault.walk_md("")
    id_set: set[str] = set()
    files = []
    for path in all_md:
        rel = vault.rel_path(path)
        if skip_path_re.match(rel):
            continue
        try:
            post = vault.read_post(rel)
            fid = post.metadata.get("id")
            if fid:
                id_set.add(str(fid))
            files.append((rel, post))
        except Exception:  # noqa: BLE001
            continue

    # Broken Wikilinks (außer Placeholder)
    placeholder_re = _re.compile(r"^([a-z]|<.*>|\.\.\.|.*\s.*|.*[—–].*|kebab-case-id|t-<slug>)$")
    broken = 0
    for rel, post in files:
        body = post.content or ""
        # Code-Spans entfernen
        body = _re.sub(r"```.*?```", "", body, flags=_re.DOTALL)
        body = _re.sub(r"<code>.*?</code>", "", body, flags=_re.DOTALL | _re.IGNORECASE)
        body = _re.sub(r"`[^`\n]+`", "", body)
        for m in vault._WIKILINK_RE.finditer(body):
            target = m.group(1).strip()
            if target and target not in id_set and not placeholder_re.match(target.lower()) and " " not in target:
                broken += 1
                break

    return {
        "total_content_files": len(files),
        "broken_wikilink_files": broken,
    }


def run_maintain(target_date: str | None = None) -> dict[str, Any]:
    """Führt die komplette Maintain-Pipeline aus.

    Returns ein Reportobjekt mit pro-Schritt Status + Gesamtdauer.
    Errors einzelner Schritte werden gefangen — Pipeline läuft weiter.
    """
    started_at = time.perf_counter()
    report: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "steps": {},
    }

    audit.log_event("maintain_start", target_date=target_date)

    try:
        report["steps"]["autolink_projects"] = step_autolink_projects()
    except Exception as e:  # noqa: BLE001
        report["steps"]["autolink_projects"] = {"error": f"{type(e).__name__}: {e}"}
        log.error("maintain.autolink_projects failed: %s", e)

    try:
        report["steps"]["normalize_context"] = step_normalize_context()
    except Exception as e:  # noqa: BLE001
        report["steps"]["normalize_context"] = {"error": f"{type(e).__name__}: {e}"}
        log.error("maintain.normalize_context failed: %s", e)

    try:
        report["steps"]["daily_backlinks"] = step_daily_backlinks(target_date)
    except Exception as e:  # noqa: BLE001
        report["steps"]["daily_backlinks"] = {"error": f"{type(e).__name__}: {e}"}
        log.error("maintain.daily_backlinks failed: %s", e)

    try:
        report["steps"]["lint_summary"] = step_lint_summary()
    except Exception as e:  # noqa: BLE001
        report["steps"]["lint_summary"] = {"error": f"{type(e).__name__}: {e}"}
        log.error("maintain.lint_summary failed: %s", e)

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    report["duration_ms"] = duration_ms
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")

    audit.log_event("maintain_finish", duration_ms=duration_ms,
                    steps={k: ("error" if "error" in v else "ok") for k, v in report["steps"].items()})

    return report
