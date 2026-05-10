"""Tests fuer die 6 Phase-X4 Query-Tools.

  - get_backlinks         (wikilinks + frontmatter related)
  - get_outgoing_links    (mit ID-Resolution)
  - list_tags             (FM + inline)
  - find_by_tag           (FM + inline)
  - find_by_property      (eq, contains, exists, in, gt, lt)
  - resolve_alias         (exact + substring)
  - get_outline           (headings, optional tables)

Run im Container:
    docker exec ki-os-mcp python /app/scripts/test_phase_x4_query_tools.py
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

        # ─── Test-Vault aufbauen ─────────────────────────────────────────────
        # File A: linkt auf B + C, hat tags, alias, properties
        write(vault_path / "10_Life" / "notes" / "alpha.md",
              "---\nid: alpha\ntitle: Alpha\ntags: [arbeit, projekt-x]\n"
              "aliases: [Erstes Note, A-Note]\nstatus: open\npriority: 3\n"
              "due: 2026-06-01\nrelated: [beta]\n---\n\n"
              "# Alpha\n\n"
              "Verlinkt auf [[beta]] und [[gamma|Gamma-Display]].\n"
              "Auch [[delta#section]] mit anchor.\n"
              "Inline-Tag #wichtig und #parent/child hier.\n\n"
              "```\nIm Code: [[ignored]] und #ignored-tag\n```\n")

        # File B: linkt zurück auf alpha
        write(vault_path / "10_Life" / "notes" / "beta.md",
              "---\nid: beta\ntitle: Beta\ntags: [arbeit]\nstatus: done\n"
              "priority: 1\n---\n\n"
              "Backlink: [[alpha]]\n#wichtig\n")

        # File C: existiert (für outgoing-resolved=True)
        write(vault_path / "10_Life" / "notes" / "gamma.md",
              "---\nid: gamma\ntitle: Gamma\nstatus: open\npriority: 5\n---\n\n# Gamma\n")

        # File D: Mehrere Headings + Tabelle (für outline)
        write(vault_path / "10_Life" / "notes" / "outline-test.md",
              "---\nid: outline-test\ntitle: Outline Test\n---\n\n"
              "# Top Heading\n\n"
              "## Section A\n\n"
              "Some text.\n\n"
              "### Sub A1\n\n"
              "## Section B\n\n"
              "| Datum | Wert |\n|---|---|\n| x | 1 |\n| y | 2 |\n\n"
              "```\n## Heading-im-Code (skip)\n```\n\n"
              "## Section C\n")

        # File E: Status-needs-review (für find_by_property in/contains)
        write(vault_path / "10_Life" / "notes" / "epsilon.md",
              "---\nid: epsilon\ntitle: Epsilon\nstatus: needs-review\n"
              "priority: 8\ntags: [zukunft]\naliases: [Eta-alt]\n---\n\nText.\n")

        # Module laden
        try:
            from ki_os_mcp import server as srv_mod
        except ImportError as e:
            print(f"SKIP: ki_os_mcp nicht installiert ({e})")
            return 0

        srv_mod.snapshot.snapshot_path = lambda *a, **k: None
        tools = srv_mod.mcp._tool_manager._tools

        from mcp.server.fastmcp.exceptions import ToolError

        backlinks = tools["get_backlinks"].fn
        outgoing = tools["get_outgoing_links"].fn
        list_tags = tools["list_tags"].fn
        find_by_tag = tools["find_by_tag"].fn
        find_by_prop = tools["find_by_property"].fn
        resolve_alias = tools["resolve_alias"].fn
        get_outline = tools["get_outline"].fn

        # ─── Test 1: get_backlinks ───────────────────────────────────────────
        print("\n=== get_backlinks ===")
        r = backlinks(path="10_Life/notes/alpha.md")
        check("target_id=alpha", r["target_id"] == "alpha")
        # beta linkt via wikilink UND related → "wikilink+related"
        beta_hit = next((h for h in r["hits"] if h["path"].endswith("beta.md")), None)
        check("beta findet alpha", beta_hit is not None)
        if beta_hit:
            check("beta via=wikilink+related",
                  beta_hit["via"] == "wikilink+related",
                  f"got {beta_hit['via']}")
            check("beta hat lines >= 1", len(beta_hit["lines"]) >= 1)

        # ─── Test 2: get_backlinks scope-Filter ──────────────────────────────
        print("\n=== get_backlinks scope ===")
        r = backlinks(path="10_Life/notes/alpha.md", scope="05_Projects")
        check("scope filtert alle weg", r["total"] == 0)

        # ─── Test 3: get_outgoing_links ──────────────────────────────────────
        print("\n=== get_outgoing_links ===")
        r = outgoing(path="10_Life/notes/alpha.md")
        # alpha linkt auf beta, gamma, delta — beta+gamma resolved, delta nicht
        targets = {l["target_id"]: l for l in r["links"]}
        check("alpha linkt auf 3 IDs", r["total"] == 3, f"got {r['total']}")
        check("beta resolved=True",
              targets.get("beta", {}).get("resolved") is True)
        check("gamma resolved=True",
              targets.get("gamma", {}).get("resolved") is True)
        check("delta resolved=False",
              targets.get("delta", {}).get("resolved") is False)
        check("Code-Block-Wikilinks ignoriert",
              "ignored" not in targets)

        # ─── Test 4: list_tags ───────────────────────────────────────────────
        print("\n=== list_tags ===")
        r = list_tags(min_count=1)
        tag_names = {t["tag"] for t in r["tags"]}
        check("'arbeit' in Tags", "arbeit" in tag_names)
        check("'wichtig' in Tags", "wichtig" in tag_names)
        check("'parent/child' nested tag", "parent/child" in tag_names)
        check("Code-Block-Tag ignoriert",
              "ignored-tag" not in tag_names)
        # arbeit ist in alpha + beta FM = 2x
        arbeit = next((t for t in r["tags"] if t["tag"] == "arbeit"), None)
        check("arbeit count=2", arbeit and arbeit["count"] == 2)
        check("arbeit nur in fm", arbeit and arbeit["sources"]["inline"] == 0)

        # ─── Test 5: list_tags min_count ─────────────────────────────────────
        print("\n=== list_tags min_count=2 ===")
        r = list_tags(min_count=2)
        names = {t["tag"] for t in r["tags"]}
        # arbeit (2x) und wichtig (2x: alpha-inline + beta-inline) sollten drin sein
        check("arbeit drin", "arbeit" in names)
        check("wichtig drin", "wichtig" in names)
        # zukunft nur 1x
        check("zukunft NICHT drin (count=1)", "zukunft" not in names)

        # ─── Test 6: find_by_tag (FM + inline) ───────────────────────────────
        print("\n=== find_by_tag ===")
        r = find_by_tag(tag="wichtig")
        paths = {h["path"] for h in r["hits"]}
        check("wichtig findet alpha (inline)",
              any(p.endswith("alpha.md") for p in paths))
        check("wichtig findet beta (inline)",
              any(p.endswith("beta.md") for p in paths))

        r = find_by_tag(tag="#arbeit")  # Auch mit #-Praefix
        check("arbeit (mit #) findet 2 files", r["total"] == 2)
        # arbeit ist nur in FM, nicht inline
        for h in r["hits"]:
            check(f"{h['path']} via=frontmatter", h["via"] == "frontmatter")

        # ─── Test 7: find_by_property eq ─────────────────────────────────────
        print("\n=== find_by_property ===")
        r = find_by_prop(field="status", value="open")
        paths = {h["path"] for h in r["hits"]}
        check("status=open findet alpha + gamma",
              len(paths) == 2 and any("alpha" in p for p in paths)
              and any("gamma" in p for p in paths),
              f"got {paths}")

        # exists
        r = find_by_prop(field="aliases", value=None, op="exists")
        paths = {h["path"] for h in r["hits"]}
        check("aliases exists: alpha + epsilon",
              any("alpha" in p for p in paths)
              and any("epsilon" in p for p in paths))

        # in
        r = find_by_prop(field="status", value=["open", "needs-review"], op="in")
        check("status in [open, needs-review] findet 3",
              r["total"] == 3, f"got {r['total']}")

        # gt (numerisch)
        r = find_by_prop(field="priority", value=4, op="gt")
        paths = {h["path"] for h in r["hits"]}
        check("priority > 4 findet gamma + epsilon",
              len(paths) == 2 and any("gamma" in p for p in paths)
              and any("epsilon" in p for p in paths),
              f"got {paths}")

        # contains list
        r = find_by_prop(field="tags", value="arbeit", op="contains")
        paths = {h["path"] for h in r["hits"]}
        check("tags contains 'arbeit' findet alpha + beta",
              len(paths) == 2)

        # ─── Test 8: resolve_alias ───────────────────────────────────────────
        print("\n=== resolve_alias ===")
        r = resolve_alias(query="A-Note")
        check("exact match findet alpha",
              r["total"] == 1 and r["hits"][0]["path"].endswith("alpha.md"))
        check("match_type=exact",
              r["hits"][0]["match_type"] == "exact")

        r = resolve_alias(query="note")  # substring → trifft "Erstes Note", "A-Note", "Eta-alt"=nein
        check("substring findet >= 1", r["total"] >= 1)
        # exact-matches kommen zuerst — substring nur substring-type
        for h in r["hits"]:
            check(f"{h['alias_matched']} matched substring",
                  "note" in h["alias_matched"].lower())

        r = resolve_alias(query="nonexistent-12345")
        check("kein match → total=0", r["total"] == 0)

        # ─── Test 9: get_outline ─────────────────────────────────────────────
        print("\n=== get_outline ===")
        r = get_outline(path="10_Life/notes/outline-test.md")
        levels = [h["level"] for h in r["headings"]]
        texts = [h["text"] for h in r["headings"]]
        check("Top Heading drin", "Top Heading" in texts)
        check("Section A drin", "Section A" in texts)
        check("Sub A1 drin (level 3)",
              "Sub A1" in texts
              and r["headings"][texts.index("Sub A1")]["level"] == 3)
        check("Heading-im-Code-Block ignoriert",
              "Heading-im-Code (skip)" not in texts)
        # Levels: 1, 2, 3, 2, 2 (Top, A, A1, B, C)
        check("Level-Hierarchie korrekt", levels == [1, 2, 3, 2, 2],
              f"got {levels}")

        # ─── Test 10: get_outline include_tables ─────────────────────────────
        print("\n=== get_outline include_tables ===")
        r = get_outline(path="10_Life/notes/outline-test.md", include_tables=True)
        check("tables key existiert", "tables" in r)
        check("1 Tabelle gefunden", len(r["tables"]) == 1)
        if r["tables"]:
            t = r["tables"][0]
            check("Spalten = [Datum, Wert]",
                  t["columns"] == ["Datum", "Wert"])
            check("n_data_rows = 2", t["n_data_rows"] == 2)

        # ─── Test 11: validators fangen falsche Inputs ───────────────────────
        print("\n=== Validator-Errors ===")
        try:
            find_by_prop(field="x", op="invalid_op", value="y")
            check("invalid_op raises", False)
        except ToolError as e:
            check("invalid_op → ToolError", "op" in str(e))

        try:
            find_by_tag(tag="123nope!")
            check("invalid tag raises", False)
        except ToolError as e:
            check("invalid tag → ToolError", "ungueltig" in str(e).lower())

        try:
            list_tags(min_count=0)
            check("min_count=0 raises", False)
        except ToolError as e:
            check("min_count=0 → ToolError", "min_count" in str(e))

    print("\n" + "=" * 50)
    if failures:
        print(f"FAIL: {failures} Tests fehlgeschlagen")
        return 1
    print("OK: alle Tests gruen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
