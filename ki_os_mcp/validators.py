"""Strict Pre-Write Validators.

Jedes Schreib-Tool ruft VOR der Operation einen passenden Validator.
Bei Verletzung: Operation wird abgebrochen mit klarer Error-Message.

Garantien:
- Schema-Konformität bevor irgendwas geschrieben wird
- Keine Drift möglich (validate-then-write Pattern)
- Klare Fehler-Messages für Tool-User
- Single Source of Truth — Tools leiten alle Schema-Decisions hierher
"""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Any

from ki_os_mcp import vault

# ---------- Konstanten ----------

VALID_PRIORITIES = {"urgent", "high", "medium", "low"}
VALID_RECURRENCE = {"daily", "weekdays", "weekly", "monthly"}
VALID_TASK_ACTIONS = {"done", "reopen", "snooze", "edit"}
VALID_TASK_STATUS = {"open", "in-progress", "blocked", "done", "cancelled", "snoozed"}
VALID_NOTE_STATUS = {"draft", "stable", "needs-review", "deprecated", "active"}
VALID_MEETING_STATUS = {"draft", "stable", "deprecated", "active"}

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------- Helper ----------


def _err(field: str, msg: str) -> str:
    return f"Validierung [{field}]: {msg}"


def _check_iso_date(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not ISO_DATE_RE.match(value):
        return _err(field, f"Erwarte ISO-Datum YYYY-MM-DD, bekommen: {value!r}")
    try:
        _date.fromisoformat(value)
    except ValueError as e:
        return _err(field, f"Ungültiges Datum: {e}")
    return None


def _check_slug(slug: str, field: str = "slug") -> str | None:
    err = vault.validate_slug(slug)
    if err:
        return _err(field, err)
    return None


def _check_string_required(value: Any, field: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return _err(field, "Pflichtfeld, darf nicht leer sein")
    return None


def _check_project_exists(project: str | None) -> str | None:
    if not project:
        return None
    project_dir = vault.VAULT_PATH / "05_Projects" / project
    if not project_dir.is_dir():
        return _err("project", f"Projekt-Folder existiert nicht: 05_Projects/{project}/")
    return None


# ---------- Validatoren pro Schreib-Tool ----------


def validate_create_note(
    title: str,
    project: str | None,
    body: str,
    tags: list[str] | None,
    subpath: str,
) -> str | None:
    """Returns Fehlertext oder None."""
    if e := _check_string_required(title, "title"): return e
    slug = vault.slugify(title)
    if e := _check_slug(slug): return e
    if e := _check_project_exists(project): return e
    if subpath not in ("notes", "meetings"):
        return _err("subpath", f"erlaubt: 'notes' oder 'meetings', bekommen: {subpath!r}")
    if tags is not None and not isinstance(tags, list):
        return _err("tags", "muss list[str] sein")
    return None


def validate_create_task(
    title: str,
    priority: str,
    due: str | None,
    context: str | None,
    project: str | None,
    recurrence: str | None,
) -> str | None:
    if e := _check_string_required(title, "title"): return e
    slug = vault.slugify(title)
    if e := _check_slug(slug): return e
    if priority not in VALID_PRIORITIES:
        return _err("priority", f"erlaubt: {sorted(VALID_PRIORITIES)}, bekommen: {priority!r}")
    if recurrence is not None and recurrence not in VALID_RECURRENCE:
        return _err("recurrence", f"erlaubt: {sorted(VALID_RECURRENCE)}, bekommen: {recurrence!r}")
    if e := _check_iso_date(due, "due"): return e
    if e := _check_project_exists(project): return e
    # Context: warnung wenn @-Präfix (wird normalisiert), aber kein Reject
    return None


def validate_create_meeting(
    title: str,
    attendees: list[str],
    project: str | None,
    date: str | None,
) -> str | None:
    if e := _check_string_required(title, "title"): return e
    slug = vault.slugify(title)
    if e := _check_slug(slug): return e
    if not attendees or not isinstance(attendees, list):
        return _err("attendees", "Pflicht — mind. 1 Teilnehmer (Schema §2 meeting)")
    if e := _check_project_exists(project): return e
    if e := _check_iso_date(date, "date"): return e
    return None


def validate_task_action(
    task_id: str,
    action: str,
    snooze_until: str | None,
    due: str | None,
    priority: str | None,
) -> str | None:
    if e := _check_string_required(task_id, "id"): return e
    if action not in VALID_TASK_ACTIONS:
        return _err("action", f"erlaubt: {sorted(VALID_TASK_ACTIONS)}, bekommen: {action!r}")
    if action == "snooze" and not snooze_until and not due:
        return _err("snooze_until", "Pflicht für action='snooze'")
    if snooze_until and (e := _check_iso_date(snooze_until, "snooze_until")): return e
    if due and (e := _check_iso_date(due, "due")): return e
    if priority and priority not in VALID_PRIORITIES:
        return _err("priority", f"erlaubt: {sorted(VALID_PRIORITIES)}")
    return None


def validate_edit_file(
    path: str,
    frontmatter_updates: dict[str, Any] | None,
    body: str | None,
) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if not path.endswith(".md"):
        return _err("path", f"edit_file nur für .md-Files (für andere: raw_write)")
    if frontmatter_updates is not None and not isinstance(frontmatter_updates, dict):
        return _err("frontmatter_updates", "muss dict sein")
    return None


def validate_move(source: str, dest: str) -> str | None:
    if e := _check_string_required(source, "source"): return e
    if e := _check_string_required(dest, "dest"): return e
    if source == dest:
        return _err("dest", "source und dest sind identisch")
    return None


def validate_goal_log(goal: str, text: str, subtype: str) -> str | None:
    if e := _check_string_required(goal, "goal"): return e
    if e := _check_string_required(text, "text"): return e
    if e := _check_string_required(subtype, "subtype"): return e
    if e := _check_slug(goal, "goal"): return e
    return None


def validate_project_context(project: str, text: str, mode: str) -> str | None:
    if e := _check_string_required(project, "project"): return e
    if e := _check_string_required(text, "text"): return e
    if mode not in ("append", "replace"):
        return _err("mode", f"erlaubt: 'append' | 'replace', bekommen: {mode!r}")
    return None


def validate_append_to_daily(text: str, section: str, date: str | None) -> str | None:
    if e := _check_string_required(text, "text"): return e
    if e := _check_string_required(section, "section"): return e
    if date and (e := _check_iso_date(date, "date")): return e
    return None


# ---------- Validators fuer Phase X3+: edit_replace, move_bulk, move_project,
#            read_project_context ---------------------------------------------


def validate_edit_file_replace(
    path: str, find: str, replace: str, regex: bool
) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if not isinstance(find, str) or not find:
        return _err("find", "Pflicht — nicht-leerer String")
    if not isinstance(replace, str):
        return _err("replace", "muss String sein (auch leerer String erlaubt)")
    if not isinstance(regex, bool):
        return _err("regex", "muss bool sein")
    return None


def validate_move_bulk(
    sources: list[str], dest_dir: str, overwrite: bool
) -> str | None:
    if not isinstance(sources, list) or not sources:
        return _err("sources", "muss nicht-leere Liste sein")
    if e := _check_string_required(dest_dir, "dest_dir"): return e
    if not isinstance(overwrite, bool):
        return _err("overwrite", "muss bool sein")
    for i, s in enumerate(sources):
        if not isinstance(s, str) or not s.strip():
            return _err(f"sources[{i}]", "leerer/nicht-string Eintrag")
    return None


def validate_move_project(slug: str, parent: str | None) -> str | None:
    if e := _check_string_required(slug, "slug"): return e
    if parent is not None and not isinstance(parent, str):
        return _err("parent", "muss str oder None sein")
    return None


def validate_read_project_context(project: str) -> str | None:
    if e := _check_string_required(project, "project"): return e
    return None


def validate_create_project(name: str, parent: str | None) -> str | None:
    if e := _check_string_required(name, "name"): return e
    slug = vault.slugify(name)
    if e := _check_slug(slug): return e
    if parent is not None and not isinstance(parent, str):
        return _err("parent", "muss str oder None sein")
    return None


# ---------- Phase-X4 Query-Tools ---------------------------------------------
# get_backlinks, get_outgoing_links, list_tags, find_by_tag, find_by_property,
# resolve_alias, get_outline


VALID_PROPERTY_OPS = {"eq", "contains", "gt", "lt", "exists", "in"}


def validate_get_backlinks(path: str, scope: str | None) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if scope is not None and not isinstance(scope, str):
        return _err("scope", "muss str oder None sein")
    return None


def validate_get_outgoing_links(path: str) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if not path.endswith(".md"):
        return _err("path", "nur fuer .md-Files")
    return None


def validate_list_tags(scope: str | None, min_count: int) -> str | None:
    if scope is not None and not isinstance(scope, str):
        return _err("scope", "muss str oder None sein")
    if not isinstance(min_count, int) or min_count < 1:
        return _err("min_count", "muss int >= 1 sein")
    return None


def validate_find_by_tag(tag: str, scope: str | None) -> str | None:
    if e := _check_string_required(tag, "tag"): return e
    # Normalisiere `#tag` → `tag` (User-Tippfehler abfangen)
    if tag.startswith("#"):
        tag = tag[1:]
    if not re.fullmatch(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9_/\-]{1,}", tag):
        return _err("tag", f"ungueltiges Tag-Format: {tag!r}")
    if scope is not None and not isinstance(scope, str):
        return _err("scope", "muss str oder None sein")
    return None


def validate_find_by_property(
    field: str, value: Any, op: str, scope: str | None
) -> str | None:
    if e := _check_string_required(field, "field"): return e
    if op not in VALID_PROPERTY_OPS:
        return _err("op", f"erlaubt: {sorted(VALID_PROPERTY_OPS)}, bekommen: {op!r}")
    if op == "exists":
        if value is not None:
            return _err("value", "fuer op='exists' muss value None sein")
    elif op == "in":
        if not isinstance(value, list) or not value:
            return _err("value", "fuer op='in' muss value eine nicht-leere Liste sein")
    else:
        if value is None:
            return _err("value", f"fuer op={op!r} ist value Pflicht")
    if scope is not None and not isinstance(scope, str):
        return _err("scope", "muss str oder None sein")
    return None


def validate_resolve_alias(query: str, scope: str | None) -> str | None:
    if e := _check_string_required(query, "query"): return e
    if scope is not None and not isinstance(scope, str):
        return _err("scope", "muss str oder None sein")
    return None


def validate_get_outline(path: str, include_tables: bool) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if not path.endswith(".md"):
        return _err("path", "nur fuer .md-Files")
    if not isinstance(include_tables, bool):
        return _err("include_tables", "muss bool sein")
    return None


# ---------- Phase-X5 Refactoring + Maintenance Tools -------------------------


def validate_list_snapshots(
    rel_path: str | None, since: str | None, until: str | None,
    op: str | None, limit: int,
) -> str | None:
    if rel_path is not None and not isinstance(rel_path, str):
        return _err("rel_path", "muss str oder None sein")
    if since and (e := _check_iso_date(since, "since")): return e
    if until and (e := _check_iso_date(until, "until")): return e
    if op is not None and (not isinstance(op, str) or not op.strip()):
        return _err("op", "wenn gesetzt: nicht-leerer String")
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return _err("limit", "int 1..500")
    return None


def validate_restore_snapshot(
    snapshot_id: str, target_path: str | None
) -> str | None:
    if e := _check_string_required(snapshot_id, "snapshot_id"): return e
    # Format: YYYY-MM-DD/HH-MM-SS_<op>_<slug>.tar.gz
    if not re.match(r"^\d{4}-\d{2}-\d{2}/\d{2}-\d{2}-\d{2}_[a-z_]+_[a-z0-9\-]+\.tar\.gz$",
                    snapshot_id):
        return _err("snapshot_id", f"Format-Mismatch: {snapshot_id!r}")
    if target_path is not None and not isinstance(target_path, str):
        return _err("target_path", "muss str oder None sein")
    return None


def validate_append_under_heading(
    path: str, heading: str, content: str, position: str, create_if_missing: bool,
) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if not path.endswith(".md"):
        return _err("path", "nur fuer .md-Files")
    if e := _check_string_required(heading, "heading"): return e
    if len(heading) > 200:
        return _err("heading", f"zu lang ({len(heading)} > 200)")
    if e := _check_string_required(content, "content"): return e
    if len(content) > 100_000:
        return _err("content", f"zu lang ({len(content)} > 100KB)")
    if position not in ("start", "end"):
        return _err("position", "erlaubt: 'start' | 'end'")
    if not isinstance(create_if_missing, bool):
        return _err("create_if_missing", "muss bool sein")
    return None


def validate_split_file(
    path: str, at_heading: str, new_path: str, copy_frontmatter: bool,
) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if not path.endswith(".md"):
        return _err("path", "nur fuer .md-Files")
    if e := _check_string_required(at_heading, "at_heading"): return e
    if e := _check_string_required(new_path, "new_path"): return e
    if not new_path.endswith(".md"):
        return _err("new_path", "muss .md sein")
    if path == new_path:
        return _err("new_path", "darf nicht identisch mit path sein")
    if not isinstance(copy_frontmatter, bool):
        return _err("copy_frontmatter", "muss bool sein")
    return None


def validate_merge_files(
    sources: list[str], target: str, mode: str, separator: str, delete_sources: bool,
) -> str | None:
    if not isinstance(sources, list) or not sources:
        return _err("sources", "muss nicht-leere Liste sein")
    if len(sources) > 50:
        return _err("sources", f"max 50 Files pro Merge ({len(sources)})")
    for i, s in enumerate(sources):
        if not isinstance(s, str) or not s.strip():
            return _err(f"sources[{i}]", "leerer/nicht-string Eintrag")
        if not s.endswith(".md"):
            return _err(f"sources[{i}]", "nur .md-Files")
    if e := _check_string_required(target, "target"): return e
    if not target.endswith(".md"):
        return _err("target", "muss .md sein")
    if target in sources:
        return _err("target", "target darf nicht in sources sein")
    if mode not in ("append", "prepend"):
        return _err("mode", "erlaubt: 'append' | 'prepend'")
    if not isinstance(separator, str):
        return _err("separator", "muss str sein (auch leer erlaubt)")
    if len(separator) > 200:
        return _err("separator", "max 200 chars")
    if not isinstance(delete_sources, bool):
        return _err("delete_sources", "muss bool sein")
    return None


def validate_apply_template(
    template_path: str, target_path: str, vars: dict[str, Any] | None,
    overwrite: bool,
) -> str | None:
    if e := _check_string_required(template_path, "template_path"): return e
    if not template_path.endswith(".md"):
        return _err("template_path", "muss .md sein")
    if e := _check_string_required(target_path, "target_path"): return e
    if not target_path.endswith(".md"):
        return _err("target_path", "muss .md sein")
    if template_path == target_path:
        return _err("target_path", "darf nicht identisch mit template_path sein")
    if vars is not None:
        if not isinstance(vars, dict):
            return _err("vars", "muss dict oder None sein")
        for k, v in vars.items():
            if not isinstance(k, str) or not k.strip():
                return _err("vars", f"Var-Key muss nicht-leerer String sein: {k!r}")
            # Variablen-Werte: alles was str() vertraegt ist OK
            if isinstance(v, (dict, list)) and any(
                isinstance(x, (dict, list)) for x in (v.values() if isinstance(v, dict) else v)
            ):
                return _err("vars", f"Var-Werte duerfen nicht verschachtelt sein: {k}")
    if not isinstance(overwrite, bool):
        return _err("overwrite", "muss bool sein")
    return None


# ---------- Phase-X6 Dashboard / Workflow-Smooth -----------------------------


def validate_vault_stats(scope: str | None, top_n_recent: int) -> str | None:
    if scope is not None and not isinstance(scope, str):
        return _err("scope", "muss str oder None sein")
    if not isinstance(top_n_recent, int) or top_n_recent < 0 or top_n_recent > 100:
        return _err("top_n_recent", "int 0..100")
    return None


def validate_get_subgraph(
    start_path: str, depth: int, max_nodes: int, include_incoming: bool,
) -> str | None:
    if e := _check_string_required(start_path, "start_path"): return e
    if not isinstance(depth, int) or depth < 1 or depth > 5:
        return _err("depth", "int 1..5 (groesser fuehrt zu Token-Explosion)")
    if not isinstance(max_nodes, int) or max_nodes < 1 or max_nodes > 200:
        return _err("max_nodes", "int 1..200")
    if not isinstance(include_incoming, bool):
        return _err("include_incoming", "muss bool sein")
    return None


def validate_random_note(
    scope: str | None, tag_filter: str | None, exclude_status: list[str] | None,
) -> str | None:
    if scope is not None and not isinstance(scope, str):
        return _err("scope", "muss str oder None sein")
    if tag_filter is not None:
        if not isinstance(tag_filter, str) or not tag_filter.strip():
            return _err("tag_filter", "wenn gesetzt: nicht-leerer String")
    if exclude_status is not None:
        if not isinstance(exclude_status, list):
            return _err("exclude_status", "muss list[str] oder None sein")
        for i, s in enumerate(exclude_status):
            if not isinstance(s, str):
                return _err(f"exclude_status[{i}]", "muss str sein")
    return None


def validate_file_audit(
    path: str, since: str | None, limit: int,
) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if since is not None:
        # Akzeptiert ISO-Datum (YYYY-MM-DD) oder ISO-Datetime (YYYY-MM-DDTHH:MM:SS)
        if not isinstance(since, str):
            return _err("since", "muss str sein")
        if not (re.match(r"^\d{4}-\d{2}-\d{2}$", since)
                or re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", since)):
            return _err("since", f"erwarte ISO-Datum oder ISO-Datetime: {since!r}")
    if not isinstance(limit, int) or limit < 1 or limit > 500:
        return _err("limit", "int 1..500")
    return None


def validate_project_overview(slug: str, recent_n: int) -> str | None:
    if e := _check_string_required(slug, "slug"): return e
    if e := _check_slug(slug): return e
    if not isinstance(recent_n, int) or recent_n < 0 or recent_n > 50:
        return _err("recent_n", "int 0..50")
    return None


def validate_append_table_row(
    path: str, values: list[str], heading: str | None
) -> str | None:
    if e := _check_string_required(path, "path"): return e
    if not path.endswith(".md"):
        return _err("path", "append_table_row nur fuer .md-Files")
    if not isinstance(values, list) or not values:
        return _err("values", "muss nicht-leere Liste sein")
    for i, v in enumerate(values):
        if not isinstance(v, str):
            return _err(f"values[{i}]", f"muss String sein (bekommen: {type(v).__name__})")
        if len(v) > 2000:
            return _err(f"values[{i}]", f"zu lang ({len(v)} > 2000 Zeichen)")
    if heading is not None:
        if not isinstance(heading, str) or not heading.strip():
            return _err("heading", "muss nicht-leerer String sein wenn gesetzt")
        if len(heading) > 200:
            return _err("heading", f"zu lang ({len(heading)} > 200 Zeichen)")
    return None
