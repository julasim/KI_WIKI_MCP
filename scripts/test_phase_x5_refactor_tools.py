"""Tests fuer die 6 Phase-X5 Refactoring + Maintenance Tools.

  - list_snapshots         (Filter: rel_path, since, until, op, limit)
  - restore_snapshot       (mit Pre-Restore-Snapshot)
  - append_under_heading   (start/end, create_if_missing)
  - split_file             (Section abspalten, Frontmatter copy)
  - merge_files            (append/prepend, Tag-Merge, delete_sources)
  - apply_template         (Auto-Vars, Custom-Vars, defaults, unresolved)

Run im Container:
    docker exec ki-os-mcp python /app/scripts/test_phase_x5_refactor_tools.py
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
        snap_dir = Path(tmp) / "snapshots"
        os.environ["VAULT_PATH"] = str(vault_path)
        os.environ["MCP_TIMEZONE"] = "UTC"
        os.environ["MCP_AUDIT_LOG"] = str(vault_path / ".audit.log")
        os.environ["MCP_SNAPSHOT_DIR"] = str(snap_dir)
        # Snapshots aktiviert (anders als andere Tests — wir wollen sie ja testen)
        os.environ["MCP_SNAPSHOT_ENABLED"] = "1"
        os.environ["MCP_TOKEN"] = "test-only"

        try:
            from ki_os_mcp import server as srv_mod
        except ImportError as e:
            print(f"SKIP: ki_os_mcp nicht installiert ({e})")
            return 0
        from mcp.server.fastmcp.exceptions import ToolError

        tools = srv_mod.mcp._tool_manager._tools
        list_snaps = tools["list_snapshots"].fn
        restore = tools["restore_snapshot"].fn
        append_h = tools["append_under_heading"].fn
        split_f = tools["split_file"].fn
        merge_f = tools["merge_files"].fn
        apply_t = tools["apply_template"].fn
        edit_replace = tools["edit_file_replace"].fn  # Fuer Snapshot-Erzeugung

        # ─── Test 1: append_under_heading end ────────────────────────────────
        print("\n=== append_under_heading end ===")
        write(vault_path / "n1.md",
              "---\nid: n1\n---\n\n"
              "# Top\n\n"
              "## Section A\n\n"
              "Existing content.\n\n"
              "## Section B\n\nB content.\n")
        r = append_h(path="n1.md", heading="Section A", content="New line!")
        content = (vault_path / "n1.md").read_text()
        # New line muss in Section A erscheinen, vor "## Section B"
        a_idx = content.find("Section A")
        new_idx = content.find("New line!")
        b_idx = content.find("Section B")
        check("Section A gefunden", a_idx > 0)
        check("New line in Section A", a_idx < new_idx < b_idx)
        check("'Existing content.' bleibt", "Existing content." in content)

        # ─── Test 2: append_under_heading start ──────────────────────────────
        print("\n=== append_under_heading start ===")
        r = append_h(path="n1.md", heading="Section B", content="Top of B!", position="start")
        content = (vault_path / "n1.md").read_text()
        # Top of B! muss VOR "B content." erscheinen
        top_b_idx = content.find("Top of B!")
        b_content_idx = content.find("B content.")
        check("Top of B! vor B content.", 0 < top_b_idx < b_content_idx)

        # ─── Test 3: append_under_heading create_if_missing ──────────────────
        print("\n=== append_under_heading create_if_missing ===")
        r = append_h(path="n1.md", heading="Logbook",
                     content="Eintrag 1", create_if_missing=True)
        content = (vault_path / "n1.md").read_text()
        check("Logbook-Heading erzeugt", "## Logbook" in content)
        check("Eintrag 1 vorhanden", "Eintrag 1" in content)
        check("created_heading=True", r["created_heading"] is True)

        # ─── Test 4: append_under_heading missing → ToolError ────────────────
        print("\n=== append_under_heading missing+create=False ===")
        try:
            append_h(path="n1.md", heading="DoesNotExist",
                     content="x", create_if_missing=False)
            check("Sollte ToolError werfen", False)
        except ToolError as e:
            check("ToolError 'nicht gefunden'", "nicht gefunden" in str(e))

        # ─── Test 5: split_file ──────────────────────────────────────────────
        print("\n=== split_file ===")
        write(vault_path / "long.md",
              "---\nid: long\ntitle: Long\ntags: [a]\n---\n\n"
              "# Top\n\nIntro.\n\n"
              "## Spinoff\n\nDetails der Spinoff-Section.\n"
              "Mehr Details.\n\n"
              "## Bleibt\n\nBleibt im Original.\n")
        r = split_f(path="long.md", at_heading="Spinoff",
                    new_path="spinoff.md", copy_frontmatter=True)
        long_content = (vault_path / "long.md").read_text()
        spin_content = (vault_path / "spinoff.md").read_text()
        check("Spinoff aus Source raus", "Spinoff-Section" not in long_content)
        check("Bleibt noch im Source", "Bleibt im Original" in long_content)
        check("Spinoff in Target", "Details der Spinoff-Section" in spin_content)
        check("Target hat eigenes id=spinoff",
              "id: spinoff" in spin_content)
        check("Target hat tags von Source",
              "- a" in spin_content or "tags:" in spin_content)

        # ─── Test 6: split_file Heading-not-found ────────────────────────────
        print("\n=== split_file missing heading ===")
        try:
            split_f(path="long.md", at_heading="GibtsNicht", new_path="x.md")
            check("Sollte ToolError werfen", False)
        except ToolError as e:
            check("ToolError 'nicht gefunden'", "nicht gefunden" in str(e))

        # ─── Test 7: merge_files append ──────────────────────────────────────
        print("\n=== merge_files append ===")
        write(vault_path / "src1.md",
              "---\nid: src1\ntags: [tag1]\n---\n\n"
              "Source1 body.\n")
        write(vault_path / "src2.md",
              "---\nid: src2\ntags: [tag2]\n---\n\n"
              "Source2 body.\n")
        write(vault_path / "tgt.md",
              "---\nid: tgt\ntags: [tag-target]\n---\n\n"
              "Target body.\n")
        r = merge_f(sources=["src1.md", "src2.md"], target="tgt.md", mode="append")
        tgt = (vault_path / "tgt.md").read_text()
        check("Target body bleibt", "Target body." in tgt)
        check("src1 angefuegt", "Source1 body." in tgt)
        check("src2 angefuegt", "Source2 body." in tgt)
        check("Tags gemerged",
              "tag-target" in tgt and "tag1" in tgt and "tag2" in tgt)
        # Reihenfolge bei append: target -> src1 -> src2
        i_t = tgt.index("Target body.")
        i_1 = tgt.index("Source1")
        i_2 = tgt.index("Source2")
        check("Reihenfolge target<src1<src2", i_t < i_1 < i_2)
        # delete_sources=False per default → sources existieren noch
        check("src1.md existiert noch", (vault_path / "src1.md").exists())

        # ─── Test 8: merge_files prepend + delete_sources ────────────────────
        print("\n=== merge_files prepend + delete_sources ===")
        write(vault_path / "p1.md", "---\nid: p1\n---\n\nP1 body.\n")
        write(vault_path / "p2.md", "---\nid: p2\n---\n\nP2 body.\n")
        write(vault_path / "ptgt.md", "---\nid: ptgt\n---\n\nPTarget body.\n")
        r = merge_f(sources=["p1.md", "p2.md"], target="ptgt.md",
                    mode="prepend", delete_sources=True)
        ptgt = (vault_path / "ptgt.md").read_text()
        i_p1 = ptgt.index("P1 body")
        i_target = ptgt.index("PTarget body")
        check("Bei prepend: src1 vor target", i_p1 < i_target)
        check("p1.md geloescht", not (vault_path / "p1.md").exists())
        check("p2.md geloescht", not (vault_path / "p2.md").exists())
        check("ptgt.md existiert noch", (vault_path / "ptgt.md").exists())

        # ─── Test 9: apply_template Auto-Vars ────────────────────────────────
        print("\n=== apply_template Auto-Vars ===")
        write(vault_path / "02_Templates" / "note.md",
              "---\nid: {{title}}\ndate: {{date}}\nstatus: draft\n---\n\n"
              "# {{title}}\n\nErstellt: {{timestamp}}\n")
        r = apply_t(template_path="02_Templates/note.md",
                    target_path="10_Life/notes/auto-test.md")
        out = (vault_path / "10_Life" / "notes" / "auto-test.md").read_text()
        check("title aufgeloest", "id: auto-test" in out)
        check("date aufgeloest (ISO)", "date: 2" in out)  # 2026-...
        check("title in body", "# auto-test" in out)
        check("timestamp aufgeloest", "Erstellt: 2" in out)
        check("vars_used enthält title", "title" in r["vars_used"])

        # ─── Test 10: apply_template Custom-Vars + defaults ──────────────────
        print("\n=== apply_template Custom-Vars + defaults ===")
        write(vault_path / "02_Templates" / "meeting.md",
              "---\nid: {{slug}}\nattendees: [{{attendee}}]\n"
              "priority: {{priority:medium}}\n---\n\n"
              "# Meeting mit {{attendee}}\n\nNotiz: {{notiz:keine}}\n")
        r = apply_t(template_path="02_Templates/meeting.md",
                    target_path="m1.md",
                    vars={"attendee": "Schmidt"})
        out = (vault_path / "m1.md").read_text()
        check("attendee custom-Var", "attendees: [Schmidt]" in out)
        check("priority default 'medium'", "priority: medium" in out)
        check("notiz default 'keine'", "Notiz: keine" in out)

        # ─── Test 11: apply_template unresolved-var ──────────────────────────
        print("\n=== apply_template unresolved ===")
        write(vault_path / "02_Templates" / "broken.md",
              "# {{title}}\n\nMissing: {{nichtdefiniert}}\n")
        r = apply_t(template_path="02_Templates/broken.md", target_path="b.md")
        out = (vault_path / "b.md").read_text()
        check("unresolved bleibt im Output", "{{nichtdefiniert}}" in out)
        check("vars_unresolved listet 'nichtdefiniert'",
              "nichtdefiniert" in r["vars_unresolved"])

        # ─── Test 12: apply_template overwrite ───────────────────────────────
        print("\n=== apply_template overwrite ===")
        try:
            apply_t(template_path="02_Templates/note.md",
                    target_path="10_Life/notes/auto-test.md")
            check("Sollte ToolError werfen (existiert)", False)
        except ToolError as e:
            check("ToolError 'existiert'", "existiert" in str(e).lower())
        # Mit overwrite=True
        r = apply_t(template_path="02_Templates/note.md",
                    target_path="10_Life/notes/auto-test.md",
                    overwrite=True)
        check("overwritten=True", r["overwritten"] is True)

        # ─── Test 13: list_snapshots + restore_snapshot ──────────────────────
        print("\n=== list_snapshots + restore ===")
        # Erst eine Aenderung erzwingen die Snapshots erzeugt
        write(vault_path / "snap-test.md",
              "---\nid: snap-test\n---\n\nORIGINAL CONTENT.\n")
        edit_replace(path="snap-test.md", find="ORIGINAL", replace="CHANGED")
        check("File ist geaendert",
              "CHANGED CONTENT" in (vault_path / "snap-test.md").read_text())

        # Snapshots auflisten
        r = list_snaps()
        check("min. 1 Snapshot da", r["total"] >= 1, f"got {r['total']}")

        # Filter nach rel_path
        r = list_snaps(rel_path="snap-test.md")
        snap_test_snaps = r["snapshots"]
        check("rel_path-Filter findet snap-test",
              len(snap_test_snaps) >= 1, f"got {len(snap_test_snaps)}")
        if snap_test_snaps:
            sid = snap_test_snaps[0]["snapshot_id"]
            check("snapshot_id Format",
                  "/" in sid and sid.endswith(".tar.gz"), f"got {sid}")
            check("files-Liste enthaelt snap-test.md",
                  "snap-test.md" in snap_test_snaps[0].get("files", []))

            # Restore
            r2 = restore(snapshot_id=sid, target_path="snap-test.md")
            check("Restore: 1 file",
                  r2["total_restored"] == 1, f"got {r2}")
            restored_text = (vault_path / "snap-test.md").read_text()
            check("File ist zurueckgerollt",
                  "ORIGINAL CONTENT" in restored_text and "CHANGED" not in restored_text)
            check("Pre-restore-snapshot existiert",
                  r2["pre_restore_snapshot"] is not None)

        # ─── Test 14: list_snapshots Validator-Errors ────────────────────────
        print("\n=== Validator-Errors ===")
        try:
            list_snaps(since="not-iso")
            check("Sollte ToolError werfen", False)
        except ToolError as e:
            check("invalid since → ToolError", "since" in str(e).lower())

        try:
            restore(snapshot_id="invalid-format.tar.gz")
            check("Sollte ToolError werfen", False)
        except ToolError as e:
            check("invalid snapshot_id → ToolError",
                  "snapshot_id" in str(e).lower() or "format" in str(e).lower())

    print("\n" + "=" * 50)
    if failures:
        print(f"FAIL: {failures} Tests fehlgeschlagen")
        return 1
    print("OK: alle Tests gruen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
