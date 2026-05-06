"""Tests fuer Phase-X3 MCP-Erweiterungen.

Testet die 4 neuen Tools gegen ein synthetisches Vault in tmpdir:
  - edit_file_replace (find/replace, regex, ReDoS-Schutz)
  - move_bulk (mehrere Files in Ordner)
  - move_project (Projekt verschieben/nesten)
  - read_project_context (CONTEXT.md lesen)

Designed fuer Container-Run: setzt voraus dass `ki_os_mcp` als pip-package
installed ist (Dockerfile macht das). Lokal ohne pip-install: SKIP.

Run im Container:
    docker exec ki-os-mcp python /app/scripts/test_phase_x3_extensions.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def main() -> int:
    failures = 0

    def check(label: str, cond: bool) -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "vault"
        # ENV VOR Import von ki_os_mcp setzen — module-level reads VAULT_PATH
        os.environ["VAULT_PATH"] = str(vault_path)
        os.environ["MCP_TIMEZONE"] = "UTC"
        os.environ["MCP_AUDIT_LOG"] = str(vault_path / ".audit.log")
        os.environ["MCP_SNAPSHOT_DIR"] = str(Path(tmp) / "snapshots")
        os.environ["MCP_SNAPSHOT_ENABLED"] = "0"
        os.environ["MCP_TOKEN"] = "test-only-token-not-used"

        # Setup minimal Vault
        write(vault_path / "10_Life" / "notes" / "test.md",
              "---\nid: test\ntitle: Test\n---\n\n# Test\n\n"
              "Heute ist Donnerstag. Vier Sachen erledigt.\n")
        write(vault_path / "05_Projects" / "alpha" / "README.md", "# Alpha\n")
        write(vault_path / "05_Projects" / "alpha" / "CONTEXT.md",
              "# Kontext: alpha\n\nDas ist Alpha.\n")
        write(vault_path / "05_Projects" / "beta" / "README.md", "# Beta\n")
        # Files for bulk-move
        write(vault_path / "01_Inbox" / "a.md", "# A\n")
        write(vault_path / "01_Inbox" / "b.md", "# B\n")
        write(vault_path / "01_Inbox" / "c.md", "# C\n")

        # Module laden — Container hat ki_os_mcp via pip-install
        try:
            from ki_os_mcp import server as srv_mod  # noqa: F401
        except ImportError as e:
            print(f"SKIP: ki_os_mcp nicht installiert ({e}) — Tests im Container laufen lassen")
            return 0

        # Snapshots disabled patchen damit Test ohne Schreib-Rechte auf
        # SNAPSHOT_DIR laueft (Tmpfs read-only in mancher CI).
        srv_mod.snapshot.snapshot_path = lambda *a, **k: None

        # Tool-Functions aus dem FastMCP-Tool-Manager raus
        tools = srv_mod.mcp._tool_manager._tools

        # Audit-Wrapper steckt zwischen uns und der echten Funktion. Holen wir
        # die original fn — falls es _audit_wrapped ist, lookup das original.
        def _raw(name: str):
            f = tools[name].fn
            return f

        edit_replace = _raw("edit_file_replace")
        move_bulk = _raw("move_bulk")
        move_project_fn = _raw("move_project")
        read_proj_ctx = _raw("read_project_context")

        # ─── Test 1: edit_file_replace literal ────────────────────────────
        print("\n=== edit_file_replace (literal) ===")
        r = edit_replace(
            path="10_Life/notes/test.md",
            find="Donnerstag",
            replace="Freitag",
        )
        check("literal: replacements=1", r["replacements"] == 1)
        check("literal: changed=True", r["changed"] is True)
        new_content = (vault_path / "10_Life" / "notes" / "test.md").read_text()
        check("literal: file enthaelt 'Freitag'", "Freitag" in new_content)
        check("literal: file enthaelt nicht mehr 'Donnerstag'", "Donnerstag" not in new_content)

        # ─── Test 2: edit_file_replace regex ──────────────────────────────
        print("\n=== edit_file_replace (regex) ===")
        r = edit_replace(
            path="10_Life/notes/test.md",
            find=r"Vier (\w+) erledigt",
            replace=r"Fünf \1 fertig",
            regex=True,
        )
        check("regex: replacements=1", r["replacements"] == 1)
        new_content = (vault_path / "10_Life" / "notes" / "test.md").read_text()
        check("regex: 'Fünf Sachen fertig' im Output", "Fünf Sachen fertig" in new_content)

        # ─── Test 3: edit_file_replace no-match (idempotent) ──────────────
        print("\n=== edit_file_replace (no-match) ===")
        r = edit_replace(
            path="10_Life/notes/test.md",
            find="xyz nicht existent abc",
            replace="bla",
        )
        check("no-match: replacements=0", r["replacements"] == 0)
        check("no-match: changed=False", r["changed"] is False)

        # ─── Test 4: edit_file_replace ReDoS-Schutz ────────────────────────
        print("\n=== edit_file_replace (ReDoS-Schutz) ===")
        try:
            edit_replace(
                path="10_Life/notes/test.md",
                find="(a+)+b",
                replace="x",
                regex=True,
            )
            check("ReDoS: pathologisches Pattern abgelehnt", False)
        except Exception as e:
            msg = str(e).lower()
            check("ReDoS: pathologisches Pattern abgelehnt",
                  "redos" in msg or "pathologisch" in msg)

        # ─── Test 5: edit_file_replace path-traversal ─────────────────────
        print("\n=== edit_file_replace (path-traversal) ===")
        try:
            edit_replace(path="../etc/passwd", find="x", replace="y")
            check("path-traversal abgelehnt", False)
        except Exception:
            check("path-traversal abgelehnt", True)

        # ─── Test 6: move_bulk ─────────────────────────────────────────────
        print("\n=== move_bulk ===")
        r = move_bulk(
            sources=["01_Inbox/a.md", "01_Inbox/b.md", "01_Inbox/c.md"],
            dest_dir="05_Projects/alpha/notes",
        )
        check("bulk: success_count=3", r["success_count"] == 3)
        check("bulk: fail_count=0", r["fail_count"] == 0)
        check("bulk: file a.md im Ziel",
              (vault_path / "05_Projects" / "alpha" / "notes" / "a.md").is_file())
        check("bulk: file in Quelle weg",
              not (vault_path / "01_Inbox" / "a.md").exists())

        # ─── Test 7: move_bulk mit non-existent ────────────────────────────
        print("\n=== move_bulk (gemischt: ok + fail) ===")
        write(vault_path / "01_Inbox" / "neu.md", "# neu\n")
        r = move_bulk(
            sources=["01_Inbox/neu.md", "01_Inbox/ghost.md"],
            dest_dir="05_Projects/alpha/notes",
        )
        check("mixed: success_count=1", r["success_count"] == 1)
        check("mixed: fail_count=1", r["fail_count"] == 1)
        check("mixed: ghost in failed",
              any("ghost" in f["name"] for f in r["failed"]))

        # ─── Test 8: move_project nest ────────────────────────────────────
        print("\n=== move_project (nest beta unter alpha) ===")
        r = move_project_fn(slug="beta", parent="alpha")
        check("nest: status=moved", r["status"] == "moved")
        check("nest: file im Ziel",
              (vault_path / "05_Projects" / "alpha" / "beta" / "README.md").is_file())
        check("nest: alte Position weg",
              not (vault_path / "05_Projects" / "beta").exists())

        # ─── Test 9: move_project zurueck top-level ───────────────────────
        print("\n=== move_project (zurueck Top-Level) ===")
        r = move_project_fn(slug="beta", parent=None)
        check("flat: status=moved", r["status"] == "moved")
        check("flat: file zurueck im Top",
              (vault_path / "05_Projects" / "beta" / "README.md").is_file())

        # ─── Test 10: move_project Schleife verhindern ────────────────────
        print("\n=== move_project (zyklus-schutz) ===")
        try:
            move_project_fn(slug="alpha", parent="alpha")
            check("zyklus: self-parent abgelehnt", False)
        except Exception as e:
            check("zyklus: self-parent abgelehnt", "selbst" in str(e).lower())

        # ─── Test 11: read_project_context ─────────────────────────────────
        print("\n=== read_project_context (existing) ===")
        r = read_proj_ctx(project="alpha")
        check("ctx: exists=True", r["exists"] is True)
        check("ctx: content enthaelt 'Alpha'", "Alpha" in r["content"])
        check("ctx: path 05_Projects/alpha/CONTEXT.md",
              r["path"] == "05_Projects/alpha/CONTEXT.md")

        # ─── Test 12: read_project_context missing project ────────────────
        print("\n=== read_project_context (kein Projekt) ===")
        r = read_proj_ctx(project="xyz-doesnt-exist")
        check("missing: exists=False", r["exists"] is False)
        check("missing: content leer", r["content"] == "")

    print()
    if failures:
        print(f"FAIL: {failures} assertion(s) failed.")
        return 1
    print("ALL PHASE-X3-EXTENSIONS TESTS OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
