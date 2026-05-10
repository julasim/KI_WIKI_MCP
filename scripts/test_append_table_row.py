"""Tests fuer append_table_row.

Test-Cases:
  1. Tabelle ohne Spacer-Row → row ans Ende
  2. Tabelle MIT trailing Spacer-Row → row VOR Spacer
  3. Mehrere Tabellen + heading → richtige Tabelle adressiert
  4. Mehrere Tabellen OHNE heading → ToolError
  5. Falsche Spaltenzahl → ToolError
  6. Keine Tabelle → ToolError
  7. Cell-Escaping: pipe und newline in values
  8. Heading nicht gefunden → ToolError
  9. Frontmatter wird preserved
 10. Heading-substring matcht
 11. Tabelle ist letzte Zeile (kein Newline am Ende des body)

Run im Container:
    docker exec ki-os-mcp python /app/scripts/test_append_table_row.py
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

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal failures
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "vault"
        os.environ["VAULT_PATH"] = str(vault_path)
        os.environ["MCP_TIMEZONE"] = "UTC"
        os.environ["MCP_AUDIT_LOG"] = str(vault_path / ".audit.log")
        os.environ["MCP_SNAPSHOT_DIR"] = str(Path(tmp) / "snapshots")
        os.environ["MCP_SNAPSHOT_ENABLED"] = "0"
        os.environ["MCP_TOKEN"] = "test-only-token-not-used"

        # Module laden
        try:
            from ki_os_mcp import server as srv_mod
        except ImportError as e:
            print(f"SKIP: ki_os_mcp nicht installiert ({e})")
            return 0

        # Snapshots disabled
        srv_mod.snapshot.snapshot_path = lambda *a, **k: None

        tools = srv_mod.mcp._tool_manager._tools
        append_row = tools["append_table_row"].fn
        from mcp.server.fastmcp.exceptions import ToolError

        # ─── Test 1: Tabelle ohne Spacer ─────────────────────────────────────
        print("\n=== Test 1: Tabelle ohne Spacer ===")
        write(vault_path / "t1.md",
              "---\nid: t1\n---\n\n"
              "| Datum | Stunden |\n|---|---|\n"
              "| 2026-04-30 | 1,25 |\n")
        r = append_row(path="t1.md", values=["2026-05-01", "2,5"])
        check("returns columns=2", r["columns"] == 2)
        check("returns total_data_rows_after=2", r["total_data_rows_after"] == 2)
        content = (vault_path / "t1.md").read_text()
        check("neue Zeile enthalten", "| 2026-05-01 | 2,5 |" in content)
        check("alte Zeile noch da", "| 2026-04-30 | 1,25 |" in content)

        # ─── Test 2: Tabelle MIT trailing Spacer ─────────────────────────────
        print("\n=== Test 2: Tabelle mit trailing Spacer ===")
        write(vault_path / "t2.md",
              "---\nid: t2\n---\n\n"
              "| A | B | C |\n|---|---|---|\n"
              "| x | y | z |\n"
              "| | | |\n")
        r = append_row(path="t2.md", values=["x2", "y2", "z2"])
        content = (vault_path / "t2.md").read_text()
        # Reihenfolge: x/y/z dann x2/y2/z2 dann Spacer
        idx_old = content.find("| x | y | z |")
        idx_new = content.find("| x2 | y2 | z2 |")
        idx_spacer = content.find("| | | |")
        check("alte Zeile vor neuer", 0 <= idx_old < idx_new,
              f"old={idx_old}, new={idx_new}")
        check("neue Zeile vor Spacer", 0 <= idx_new < idx_spacer,
              f"new={idx_new}, spacer={idx_spacer}")
        # Spacer zaehlt nicht als Daten-Zeile
        check("data_rows=2 (Spacer nicht mitgezaehlt)", r["total_data_rows_after"] == 2)

        # ─── Test 3: Mehrere Tabellen + heading ──────────────────────────────
        print("\n=== Test 3: Mehrere Tabellen + heading ===")
        write(vault_path / "t3.md",
              "---\nid: t3\n---\n\n"
              "## Stunden\n\n"
              "| Datum | h |\n|---|---|\n"
              "| 2026-04-30 | 1 |\n\n"
              "## Materialien\n\n"
              "| Item | Anzahl |\n|---|---|\n"
              "| Schraube | 5 |\n")
        r = append_row(path="t3.md", values=["2026-05-01", "2"], heading="Stunden")
        content = (vault_path / "t3.md").read_text()
        # Neue Zeile MUSS in Stunden-Section sein, NICHT in Materialien
        stunden_idx = content.find("## Stunden")
        material_idx = content.find("## Materialien")
        new_idx = content.find("| 2026-05-01 | 2 |")
        check("neue Zeile in Stunden-Section",
              stunden_idx < new_idx < material_idx,
              f"st={stunden_idx}, new={new_idx}, mat={material_idx}")

        # ─── Test 4: Mehrere Tabellen ohne heading → Error ───────────────────
        print("\n=== Test 4: Mehrere Tabellen ohne heading ===")
        try:
            append_row(path="t3.md", values=["x", "y"])
            check("ToolError raised", False)
        except ToolError as e:
            check("ToolError mit 'Mehrere Tabellen'", "Mehrere Tabellen" in str(e),
                  f"got: {e}")

        # ─── Test 5: Falsche Spaltenzahl ─────────────────────────────────────
        print("\n=== Test 5: Falsche Spaltenzahl ===")
        try:
            append_row(path="t1.md", values=["nur", "drei", "stuecke"])
            check("ToolError raised", False)
        except ToolError as e:
            check("ToolError mit 'Spaltenzahl-Mismatch'",
                  "Spaltenzahl-Mismatch" in str(e), f"got: {e}")

        # ─── Test 6: Keine Tabelle ───────────────────────────────────────────
        print("\n=== Test 6: Keine Tabelle ===")
        write(vault_path / "t6.md",
              "---\nid: t6\n---\n\nNur Prosa hier.\nKein Pipe.\n")
        try:
            append_row(path="t6.md", values=["x"])
            check("ToolError raised", False)
        except ToolError as e:
            check("ToolError 'Keine Markdown-Tabelle'",
                  "Keine Markdown-Tabelle" in str(e), f"got: {e}")

        # ─── Test 7: Cell-Escaping ───────────────────────────────────────────
        print("\n=== Test 7: Cell-Escaping (pipe + newline) ===")
        write(vault_path / "t7.md",
              "---\nid: t7\n---\n\n"
              "| Wert |\n|---|\n"
              "| ok |\n")
        r = append_row(path="t7.md", values=["a|b\nc"])
        content = (vault_path / "t7.md").read_text()
        check("pipe escaped (\\|)", r"a\|b" in content)
        check("newline → <br>", "<br>c" in content)
        # Tabelle muss noch parsebar sein (keine zusaetzliche Pipe als Zellen-Trenner)
        new_line_count = content.count("| a\\|b<br>c |")
        check("eine valide Zeile geschrieben", new_line_count == 1)

        # ─── Test 8: Heading nicht gefunden ──────────────────────────────────
        print("\n=== Test 8: Heading nicht gefunden ===")
        try:
            append_row(path="t1.md", values=["x", "y"], heading="Existiert-nicht")
            check("ToolError raised", False)
        except ToolError as e:
            check("ToolError 'Heading nicht gefunden'",
                  "Heading nicht gefunden" in str(e), f"got: {e}")

        # ─── Test 9: Frontmatter preserved ───────────────────────────────────
        print("\n=== Test 9: Frontmatter preserved ===")
        write(vault_path / "t9.md",
              "---\nid: t9\ntitle: Mein Titel\ntags: [a, b]\n---\n\n"
              "| Datum | Wert |\n|---|---|\n"
              "| 2026-04-30 | 1 |\n")
        append_row(path="t9.md", values=["2026-05-01", "2"])
        content = (vault_path / "t9.md").read_text()
        check("id preserved", "id: t9" in content)
        check("title preserved", "title: Mein Titel" in content)
        check("tags preserved", "tags:" in content and "- a" in content)
        # `updated` wird automatisch gesetzt
        check("updated gesetzt", "updated:" in content)

        # ─── Test 10: Heading-Substring matcht ───────────────────────────────
        print("\n=== Test 10: Heading-Substring ===")
        write(vault_path / "t10.md",
              "---\nid: t10\n---\n\n"
              "## Stundenaufzeichnung 2026\n\n"
              "| Datum | h |\n|---|---|\n"
              "| 2026-04-30 | 1 |\n\n"
              "## Andere\n\n| x | y |\n|---|---|\n| a | b |\n")
        r = append_row(path="t10.md",
                       values=["2026-05-01", "2"],
                       heading="Stundenaufzeichnung")  # substring-match
        check("heading substring funktioniert", r["columns"] == 2)

        # ─── Test 11: Tabelle ist letzte Zeile (kein trailing \n) ────────────
        print("\n=== Test 11: Tabelle ohne trailing-newline ===")
        # frontmatter.dumps schreibt mit trailing \n — daher manuell raw schreiben
        # via vault.read_post → write_post fuer den Test
        write(vault_path / "t11.md",
              "---\nid: t11\n---\n\n"
              "| A |\n|---|\n| x |")
        r = append_row(path="t11.md", values=["y"])
        content = (vault_path / "t11.md").read_text()
        check("neue Zeile da", "| y |" in content)

    print("\n" + "=" * 50)
    if failures:
        print(f"FAIL: {failures} Tests fehlgeschlagen")
        return 1
    print("OK: alle Tests gruen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
