"""Vault-Autopilot — Self-Maintenance-Pipeline.

Ein zentraler Run der alle Vault-Pflege-Operationen idempotent ausführt.
Wird automatisch getriggert nach jedem Schreibvorgang (immediate, async)
plus periodisch (alle 10 Min + Boot).

PIPELINE-SCHRITTE
=================
1. Auto-Link alle Projekt-READMEs (vault_autolink)
2. Context-Drift normalisieren (Tasks: @-Präfix einheitlich)
3. Daily-Backlinks: heute erstellte Notes/Meetings/Tasks in Daily-Note
4. Recurring-Tasks reaktivieren (daily/weekdays/weekly/monthly fällig)
5. Daily-Skeleton für morgen pre-create (idempotent)
6. Goal-Status-Check (Säulen-Drift, Habits/Sport-Score)
7. Lint-Report: was nicht auto-fixbar war

GARANTIEN
=========
- Idempotent: jeder Schritt kann beliebig oft laufen, kein Drift
- Sicher: bei Exception wird der Schritt geskippt, andere laufen weiter
- Audit: jeder Run loggt Start, Dauer, Aktionen, Errors
- Rollback: bei kritischem Fehler Snapshot vor Lauf
- Timezone-aware: alle "today"-Checks nutzen Europe/Vienna (Bot-konform)
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ki_os_mcp import audit, vault

log = logging.getLogger("ki-os-mcp.maintain")

# Timezone für "heute"-Checks. Container läuft in UTC, aber recurring-Logik
# muss Wien-lokal denken (sonst reaktivieren Daily-Tasks nach 22:00 Wien
# nicht — `date.today()` wäre da schon morgen).
TIMEZONE = ZoneInfo(os.environ.get("MCP_TIMEZONE", "Europe/Vienna"))


def today_local() -> date:
    """Aktuelles Datum in Wien-Zeit (TZ-konsistent mit dem Bot)."""
    return datetime.now(TIMEZONE).date()


# Recurring-Task-Patterns die reactivate-Logic versteht.
VALID_RECURRENCE = {"daily", "weekdays", "weekly", "monthly"}


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


# ---------- Recurring-Tasks ---------------------------------------------------


def _parse_iso_date(value: Any) -> date | None:
    """Akzeptiert str ('YYYY-MM-DD') oder date-Objekt — beide kommen aus YAML."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def is_recurrence_due(pattern: str, last_completed: Any, today: date) -> bool:
    """Bestimmt ob eine recurring Task heute reaktiviert werden soll.

    Pattern: 'daily' | 'weekdays' | 'weekly' | 'monthly'
    last_completed: ISO-Datum (str) ODER date-Objekt (PyYAML kann unquoted
                    YYYY-MM-DD direkt zu date parsen).

    Logic 1:1 aus Bot.ki_wiki_bot._is_recurrence_due — Single Source of Truth
    bleibt erhalten wenn Bot in Phase X3 die Logik abgibt.
    """
    last = _parse_iso_date(last_completed)
    if last is None or last >= today:
        return False  # nie done oder schon zukünftig

    if pattern == "daily":
        return True
    if pattern == "weekdays":
        return today.weekday() < 5  # Mo-Fr
    if pattern == "weekly":
        return (today - last).days >= 7
    if pattern == "monthly":
        if today.month == last.month and today.year == last.year:
            return False
        # Letzten Tag des aktuellen Monats berechnen für 31er-Tasks im Feb
        if today.month == 12:
            next_first = date(today.year + 1, 1, 1)
        else:
            next_first = date(today.year, today.month + 1, 1)
        last_day_of_month = (next_first - timedelta(days=1)).day
        target_day = min(last.day, last_day_of_month)
        return today.day >= target_day
    return False


def step_task_reactivate_recurring() -> dict[str, Any]:
    """Schritt: Recurring-Tasks reaktivieren.

    Walked 10_Life/tasks/*.md, findet Tasks mit:
      - status == 'done'
      - recurrence in {daily, weekdays, weekly, monthly}
      - last_completed liegt in der Vergangenheit + Pattern fällig

    Setzt status auf 'open', appendet "- YYYY-MM-DD: reaktiviert" ins Body.
    Idempotent: re-run findet die schon-reaktivierten Tasks (status=open) und
    skippt sie.
    """
    tasks_dir = vault.VAULT_PATH / "10_Life" / "tasks"
    if not tasks_dir.is_dir():
        return {"checked": 0, "reactivated": []}

    today = today_local()
    today_str = today.isoformat()
    checked = 0
    reactivated: list[str] = []
    errors: list[str] = []

    for path in sorted(tasks_dir.glob("*.md")):
        checked += 1
        try:
            post = vault.read_post(vault.rel_path(path))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path.name}: read: {type(e).__name__}: {e}")
            continue
        meta = post.metadata
        recurrence = meta.get("recurrence")
        if not recurrence or recurrence not in VALID_RECURRENCE:
            continue
        if meta.get("status") != "done":
            continue
        last_completed = meta.get("last_completed")
        if not is_recurrence_due(recurrence, last_completed, today):
            continue

        # Reaktivieren
        try:
            post["status"] = "open"
            post["updated"] = today_str
            body = (post.content or "").rstrip() + (
                f"\n- {today_str}: reaktiviert (recurring={recurrence})\n"
            )
            post.content = body
            vault.write_post(vault.rel_path(path), post)
            reactivated.append(path.stem)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path.name}: write: {type(e).__name__}: {e}")

    return {
        "checked": checked,
        "reactivated": reactivated,
        "count": len(reactivated),
        "errors": errors,
    }


# ---------- Daily-Skeleton-Pre-Create -----------------------------------------


def step_create_daily_skeleton(target_date: str | None = None) -> dict[str, Any]:
    """Schritt: Daily-Note für morgen (oder target_date) pre-create.

    Idempotent: wenn die Daily-Note schon existiert, wird sie nicht überschrieben.
    Default target_date = morgen (Wien-Zeit), damit User morgens schon die
    fertige Daily mit FM-Skeleton hat.

    TOCTOU-safe: O_EXCL — falls parallel ein anderer Caller (z.B. User-Bot-
    Message um 23:59) die Daily anlegt, gibt's keine Race-Condition.
    """
    if target_date:
        try:
            target = date.fromisoformat(target_date)
        except ValueError:
            return {"error": f"Ungültiges Datum: {target_date!r}"}
    else:
        target = today_local() + timedelta(days=1)

    iso = target.isoformat()
    rel = f"10_Life/daily/{iso}.md"
    p = vault.safe_path(rel)
    # Kein exists()-Check vor O_EXCL — das ist die einzige Race-Free-Variante.
    # Wir vertrauen O_EXCL: bei Existenz gibt's FileExistsError, sonst Datei
    # wird atomic erzeugt. Damit kein TOCTOU.
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Skeleton-Content vorbereiten (gleiche Struktur wie vault._create_daily_skeleton)
        import yaml  # type: ignore
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
        content = (
            "---\n"
            + yaml.safe_dump(fm, allow_unicode=True, sort_keys=True)
            + "---\n"
            + body
        )
        # O_EXCL: nur erzeugen wenn noch nicht da (atomic create-or-skip)
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            return {"date": iso, "path": rel, "created": True}
        except FileExistsError:
            return {"date": iso, "path": rel, "created": False,
                    "reason": "race_already_exists"}
    except Exception as e:  # noqa: BLE001
        return {"date": iso, "path": rel, "created": False,
                "error": f"{type(e).__name__}: {e}"}


# ---------- Goal-Status-Check -------------------------------------------------


# Drift-Schwellen (Tage seit letztem Anker → Status-Severity)
DRIFT_THRESHOLDS = {
    "weekly": 14,    # > 14d ohne Wochen-Anker → warn
    "monthly": 60,   # > 60d ohne Monats-Anker → warn
    "quarterly": 120,  # > 120d ohne Quartals-Anker → warn
}


def step_goal_status_check() -> dict[str, Any]:
    """Schritt: Goal-System-Drift-Check.

    Prüft 10_Life/goals/5y-2031/readme.md auf Drift-Anker-Alter und liefert
    pro Bucket einen Status (ok/warn) + Tage-seit-Anker.

    Plus: Habits-Score (letzte 7d) und Sport-Score (letzte 7d/30d).

    Return ist read-only — schreibt nichts. Dient als Diagnose-Output für
    /health + Bot-Briefing.
    """
    goal_base = vault.goal_base()
    today = today_local()
    out: dict[str, Any] = {"date": today.isoformat()}

    # --- Drift-Anker aus readme.md ---
    readme = goal_base / "readme.md"
    drift: dict[str, Any] = {}
    if readme.is_file():
        raw = readme.read_text(encoding="utf-8").replace("\r\n", "\n")
        for label, key in (
            ("Letzter Wochen-Anker", "weekly"),
            ("Letzter Monats-Anker", "monthly"),
            ("Letzter Quartals-Anker", "quarterly"),
        ):
            m = re.search(rf"\*\*{re.escape(label)}:\*\*\s*([\d-]+|—)", raw)
            anker = m.group(1) if m else None
            entry: dict[str, Any] = {"value": anker}
            if anker and anker != "—":
                d = _parse_iso_date(anker)
                if d:
                    age = (today - d).days
                    entry["age_days"] = age
                    entry["status"] = "warn" if age > DRIFT_THRESHOLDS[key] else "ok"
                else:
                    entry["status"] = "unknown"
            else:
                entry["status"] = "warn"  # nie gesetzt = drift
                entry["age_days"] = None
            drift[key] = entry
    out["drift"] = drift

    # --- Habits-Score letzte 7 Tage ---
    habits_path = goal_base / "tracker" / "habits.md"
    habits_score: dict[str, Any] = {"check": 0, "possible": 0, "pct": 0}
    if habits_path.is_file():
        raw = habits_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        cutoff = today - timedelta(days=7)
        check = 0
        possible = 0
        for line in raw.split("\n"):
            m = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.+)\|$", line)
            if not m:
                continue
            try:
                line_date = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if line_date < cutoff or line_date > today:
                continue
            cells = [c.strip() for c in m.group(2).split("|")]
            for c in cells[:6]:
                if c == "✓":
                    check += 1
                if c in ("✓", "✗"):
                    possible += 1
        pct = int(check / possible * 100) if possible > 0 else 0
        habits_score = {
            "check": check,
            "possible": possible,
            "pct": pct,
            "status": "ok" if pct >= 80 else "warn",
        }
    out["habits_7d"] = habits_score

    # --- Sport-Sessions letzte 7d/30d ---
    sport_path = goal_base / "tracker" / "sport-log.md"
    sport_count = {"d7": 0, "d30": 0}
    if sport_path.is_file():
        raw = sport_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        cutoff_7 = today - timedelta(days=7)
        cutoff_30 = today - timedelta(days=30)
        for line in raw.split("\n"):
            m = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(cardio|kraft)\s*\|", line)
            if not m:
                continue
            try:
                d = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if d > today:
                continue
            if d >= cutoff_30:
                sport_count["d30"] += 1
            if d >= cutoff_7:
                sport_count["d7"] += 1
    out["sport"] = {
        **sport_count,
        "status": "ok" if sport_count["d7"] >= 3 else "warn",  # Wochen-Soll: 3
    }

    # --- Overall-Severity ---
    statuses = [drift[k].get("status") for k in drift]
    statuses.append(habits_score.get("status"))
    statuses.append(out["sport"].get("status"))
    out["overall"] = "warn" if "warn" in statuses else "ok"

    return out


# ---------- Pipeline-Driver ---------------------------------------------------


def step_oauth_codes_cleanup() -> dict[str, Any]:
    """Schritt: abgelaufene OAuth-Authorization-Codes aus dem in-memory Store
    entfernen. Verhindert Memory-Leak bei Code-Issuing ohne Code-Consume.
    """
    try:
        from ki_os_mcp import oauth
        if not oauth.is_configured():
            return {"removed": 0, "skipped": "oauth_disabled"}
        n = oauth.cleanup_expired_codes()
        return {"removed": n}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


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

    def _run_step(name: str, fn, *args) -> None:
        try:
            report["steps"][name] = fn(*args)
        except Exception as e:  # noqa: BLE001
            report["steps"][name] = {"error": f"{type(e).__name__}: {e}"}
            log.error("maintain.%s failed: %s", name, e)

    _run_step("autolink_projects", step_autolink_projects)
    _run_step("normalize_context", step_normalize_context)
    _run_step("daily_backlinks", step_daily_backlinks, target_date)
    _run_step("task_reactivate_recurring", step_task_reactivate_recurring)
    _run_step("create_daily_skeleton", step_create_daily_skeleton, None)
    _run_step("goal_status_check", step_goal_status_check)
    _run_step("oauth_codes_cleanup", step_oauth_codes_cleanup)
    _run_step("lint_summary", step_lint_summary)

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    report["duration_ms"] = duration_ms
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")

    audit.log_event("maintain_finish", duration_ms=duration_ms,
                    steps={k: ("error" if "error" in v else "ok") for k, v in report["steps"].items()})

    return report
