"""Backup-Snapshots vor destruktiven Vault-Operationen.

Vor jedem `move`, `confirm_delete`, `edit_file`-Body-Replace wird ein
tar.gz-Archiv der betroffenen Files in MCP_SNAPSHOT_DIR angelegt.
Recovery: einfach Tar entpacken über Vault.

Snapshot-Pfad-Schema:
  {MCP_SNAPSHOT_DIR}/{YYYY-MM-DD}/{HH-MM-SS}_{op}_{slug}.tar.gz

Default MCP_SNAPSHOT_DIR = /snapshots (im Container; Host-Mount).
"""

from __future__ import annotations

import logging
import os
import re
import tarfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

log = logging.getLogger("ki-os-mcp.snapshot")

SNAPSHOT_DIR = Path(os.environ.get("MCP_SNAPSHOT_DIR", "/snapshots"))
SNAPSHOT_ENABLED = os.environ.get("MCP_SNAPSHOT_ENABLED", "1") not in ("0", "false", "no")

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _safe_slug(text: str) -> str:
    s = text.lower().replace("/", "-").replace("\\", "-")
    s = _SLUG_RE.sub("-", s).strip("-")
    return s[:60]


def snapshot(op: str, files: dict[str, bytes]) -> str | None:
    """Erstellt ein tar.gz mit den übergebenen Files (rel-path → bytes).

    Args:
        op: Operation-Name (z.B. "move", "delete", "edit")
        files: dict {rel_path: file_bytes}

    Returns:
        Pfad zum erstellten Snapshot (rel zu SNAPSHOT_DIR), oder None bei Fehler.
    """
    if not SNAPSHOT_ENABLED:
        return None
    if not files:
        return None
    try:
        now = datetime.now()
        day_dir = SNAPSHOT_DIR / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        # Slug aus erstem File-Pfad
        first_path = next(iter(files))
        slug = _safe_slug(Path(first_path).stem or "vault")
        ts = now.strftime("%H-%M-%S")
        out = day_dir / f"{ts}_{op}_{slug}.tar.gz"

        with tarfile.open(out, "w:gz") as tar:
            for rel, content in files.items():
                info = tarfile.TarInfo(name=rel)
                info.size = len(content)
                info.mtime = int(now.timestamp())
                tar.addfile(info, BytesIO(content))

        rel_out = str(out.relative_to(SNAPSHOT_DIR)).replace("\\", "/")
        log.info("Snapshot created: %s (%d files)", rel_out, len(files))
        return rel_out
    except OSError as e:
        log.error("Snapshot failed for op=%s: %s", op, e)
        return None


def snapshot_path(rel_path: str, content: bytes, op: str) -> str | None:
    """Convenience: Snapshot eines einzelnen Files."""
    return snapshot(op, {rel_path: content})


def snapshot_paths(paths_with_content: list[tuple[str, bytes]], op: str) -> str | None:
    """Convenience: Snapshot mehrerer Files."""
    return snapshot(op, dict(paths_with_content))


# ---------- Recovery (list + restore) ---------------------------------------


# snapshot_id Format: `<YYYY-MM-DD>/<HH-MM-SS>_<op>_<slug>.tar.gz`
#
# WICHTIG: op ist als CLOSED SET implementiert. Der naive Regex
# `([a-z_]+)_([a-z0-9\-]+)` matcht greedy, sodass `edit_replace_my-slug`
# als op=edit_replace_my, slug=slug interpretiert wird (off-by-1). Die
# Liste hier muss bei jedem neuen `snapshot(...)` / `snapshot_path(...)`
# Aufruf mit-aktualisiert werden — sonst werden neue Snapshots vom
# regex nicht gematcht und in list_snapshots ignoriert.
SNAPSHOT_OPS = (
    "edit",
    "edit_replace",
    "append_table_row",
    "append_under_heading",
    "apply_template",
    "delete",
    "move",
    "merge",
    "split_source",
    "raw_write",
    "pre_restore",
)
_SNAPSHOT_NAME_RE = re.compile(
    r"^(\d{2}-\d{2}-\d{2})_(" + "|".join(SNAPSHOT_OPS) + r")_([a-z0-9\-]+)\.tar\.gz$"
)


def list_snapshots(
    *,
    rel_path: str | None = None,
    since: str | None = None,
    until: str | None = None,
    op: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Listet Snapshots, optional gefiltert.

    Filter:
        rel_path: nur Snapshots die diesen Vault-Pfad enthalten (tar-content-check)
        since:    ISO-Datum YYYY-MM-DD (nur Snapshots ab diesem Tag)
        until:    ISO-Datum YYYY-MM-DD (nur Snapshots bis incl. diesem Tag)
        op:       z.B. "edit", "edit_replace", "move", "delete"
        limit:    Maximalzahl (jüngste zuerst)

    Returns:
        Liste von dicts mit:
            snapshot_id, day, time, op, slug, size, mtime,
            files (Liste der Pfade im tar — nur wenn rel_path-Filter aktiv
            ODER wir das tar fuer den Detail-View ohnehin oeffnen muessen)
    """
    if not SNAPSHOT_DIR.is_dir():
        return []
    out: list[dict[str, object]] = []
    # Day-Folders sortiert absteigend
    days = sorted([d for d in SNAPSHOT_DIR.iterdir() if d.is_dir()], reverse=True)
    for day_dir in days:
        day_name = day_dir.name
        if since and day_name < since:
            continue
        if until and day_name > until:
            continue
        # Snapshots in diesem Tag absteigend nach mtime
        snaps = sorted(
            day_dir.glob("*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for tar_path in snaps:
            m = _SNAPSHOT_NAME_RE.match(tar_path.name)
            if not m:
                continue
            time_part, op_part, slug = m.group(1), m.group(2), m.group(3)
            if op and op_part != op:
                continue

            files_in_tar: list[str] = []
            include_file_list = rel_path is not None
            if include_file_list:
                try:
                    with tarfile.open(tar_path, "r:gz") as tar:
                        files_in_tar = [m.name for m in tar.getmembers() if m.isfile()]
                except (tarfile.TarError, OSError):
                    continue
                if rel_path not in files_in_tar:
                    continue

            stat = tar_path.stat()
            entry: dict[str, object] = {
                "snapshot_id": f"{day_name}/{tar_path.name}",
                "day": day_name,
                "time": time_part.replace("-", ":"),
                "op": op_part,
                "slug": slug,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
            if include_file_list:
                entry["files"] = files_in_tar
            out.append(entry)
            if len(out) >= limit:
                return out
    return out


def restore_snapshot(
    snapshot_id: str,
    *,
    target_path: str | None = None,
    vault_root: Path,
) -> list[dict[str, object]]:
    """Stellt Files aus einem Snapshot wieder her.

    Args:
        snapshot_id: Format `<YYYY-MM-DD>/<HH-MM-SS>_<op>_<slug>.tar.gz`
        target_path: Wenn gesetzt, NUR dieses File aus dem Snapshot
                     wiederherstellen (rel zum Vault). Wenn None, alle.
        vault_root: Absoluter Pfad zum Vault — Files werden dort hin
                    extrahiert (absichtlich Pflicht-Param, kein Default,
                    damit man's beim Aufruf bewusst sieht).

    Returns:
        Liste der wiederhergestellten Files: [{path, bytes_written, existed_before}]

    Raises:
        FileNotFoundError, OSError bei Tar-Problemen.
    """
    # Path-Traversal-Schutz: snapshot_id darf keine `..` enthalten
    if ".." in snapshot_id or snapshot_id.startswith("/"):
        raise ValueError(f"Ungueltige snapshot_id: {snapshot_id!r}")
    tar_path = (SNAPSHOT_DIR / snapshot_id).resolve()
    try:
        tar_path.relative_to(SNAPSHOT_DIR.resolve())
    except ValueError as e:
        raise ValueError(f"snapshot_id ausserhalb SNAPSHOT_DIR: {snapshot_id}") from e
    if not tar_path.is_file():
        raise FileNotFoundError(f"Snapshot nicht gefunden: {snapshot_id}")

    restored: list[dict[str, object]] = []
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name
            if target_path and rel != target_path:
                continue
            # Path-Traversal-Schutz im Tar
            dest = (vault_root / rel).resolve()
            try:
                dest.relative_to(vault_root.resolve())
            except ValueError:
                log.warning("Skip tar-member outside vault: %s", rel)
                continue
            existed = dest.exists()
            f = tar.extractfile(member)
            if f is None:
                continue
            data = f.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            restored.append({
                "path": rel,
                "bytes_written": len(data),
                "existed_before": existed,
            })

    if target_path and not restored:
        raise FileNotFoundError(
            f"target_path {target_path!r} nicht im Snapshot {snapshot_id}"
        )

    log.info("Restored from snapshot %s: %d files", snapshot_id, len(restored))
    return restored
