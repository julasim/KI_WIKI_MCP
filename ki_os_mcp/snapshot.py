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
