"""Tests fuer die 5 Phase-X6 Dashboard / Workflow-Smooth Tools.

  - vault_stats         (counts, by_type, by_status, recent)
  - get_subgraph        (BFS outgoing + incoming, depth, max_nodes, truncated)
  - random_note         (scope, tag_filter, exclude_status)
  - file_audit          (filter by path, since)
  - project_overview    (counts, hours_total, recent_notes, tasks_open_top)

Run im Container:
    docker exec ki-os-mcp python /app/scripts/test_phase_x6_dashboard_tools.py
"""

from __future__ import annotations

import json
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
        # Audit-Log fuer file_audit-Test
        audit_log = vault_path / ".audit.log"
        os.environ["MCP_AUDIT_LOG"] = str(audit_log)
        os.environ["MCP_SNAPSHOT_DIR"] = str(Path(tmp) / "snapshots")
        os.environ["MCP_SNAPSHOT_ENABLED"] = "0"
        os.environ["MCP_TOKEN"] = "test-only"

        # Test-Vault aufbauen
        write(vault_path / "10_Life" / "notes" / "alpha.md",
              "---\nid: alpha\ntitle: Alpha\ntype: note\nstatus: draft\n"
              "tags: [arbeit]\n---\n\n# Alpha\n\n"
              "Linkt auf [[beta]] und [[gamma]].\n")
        write(vault_path / "10_Life" / "notes" / "beta.md",
              "---\nid: beta\ntitle: Beta\ntype: note\nstatus: stable\n"
              "tags: [zukunft]\n---\n\n# Beta\n\nLink: [[gamma]].\n")
        write(vault_path / "10_Life" / "notes" / "gamma.md",
              "---\nid: gamma\ntitle: Gamma\ntype: note\nstatus: deprecated\n"
              "tags: [arbeit]\n---\n\n# Gamma\n")
        write(vault_path / "10_Life" / "tasks" / "task-1.md",
              "---\nid: t-1\ntitle: Task 1\ntype: task\nstatus: open\n"
              "priority: high\nproject: alpha-proj\n---\n\nDo it.\n")
        write(vault_path / "10_Life" / "tasks" / "task-2.md",
              "---\nid: t-2\ntitle: Task 2\ntype: task\nstatus: done\n"
              "project: alpha-proj\n---\n\nDone.\n")
        write(vault_path / "10_Life" / "tasks" / "task-3.md",
              "---\nid: t-3\ntitle: Task 3\ntype: task\nstatus: open\n"
              "priority: urgent\nproject: alpha-proj\ndue: 2026-05-09\n---\n\nUrgent.\n")
        # Projekt
        write(vault_path / "05_Projects" / "alpha-proj" / "README.md",
              "---\nid: alpha-proj\ntitle: Alpha-Projekt\ntype: project\nstatus: active\n---\n\n"
              "# Alpha-Projekt\n")
        write(vault_path / "05_Projects" / "alpha-proj" / "CONTEXT.md",
              "Das ist der Kontext-Text fuer Alpha-Projekt. Sehr wichtig.\n")
        write(vault_path / "05_Projects" / "alpha-proj" / "notes" / "n1.md",
              "---\nid: alpha-proj-n1\ntitle: Note 1\ntype: note\nproject: alpha-proj\n"
              "date: 2026-05-08\n---\n\nNote-1-Content.\n")
        write(vault_path / "05_Projects" / "alpha-proj" / "stundenaufzeichnung.md",
              "---\nid: stunden-alpha\ntype: note\n---\n\n"
              "| Datum | Stunden | Beschreibung |\n|---|---|---|\n"
              "| 2026-05-07 | 2,5 | Arbeit A |\n"
              "| 2026-05-08 | 3,0 | Arbeit B |\n")

        try:
            from ki_os_mcp import server as srv_mod
        except ImportError as e:
            print(f"SKIP: ki_os_mcp nicht installiert ({e})")
            return 0
        from mcp.server.fastmcp.exceptions import ToolError

        srv_mod.snapshot.snapshot_path = lambda *a, **k: None

        tools = srv_mod.mcp._tool_manager._tools
        vault_stats = tools["vault_stats"].fn
        get_subgraph = tools["get_subgraph"].fn
        random_note = tools["random_note"].fn
        file_audit = tools["file_audit"].fn
        project_overview = tools["project_overview"].fn

        # ─── Test 1: vault_stats ─────────────────────────────────────────────
        print("\n=== vault_stats ===")
        r = vault_stats()
        check("total_files >= 7", r["total_files"] >= 7, f"got {r['total_files']}")
        check("total_words > 0", r["total_words"] > 0)
        check("by_type: note >= 4", r["by_type"].get("note", 0) >= 4)
        check("by_type: task = 3", r["by_type"].get("task", 0) == 3)
        check("by_status: draft = 1", r["by_status"].get("draft", 0) == 1)
        check("tasks.open = 2", r["tasks"]["open"] == 2,
              f"got {r['tasks']}")
        check("tasks.done = 1", r["tasks"]["done"] == 1)
        check("tasks.total = 3", r["tasks"]["total"] == 3)
        check("recent_modifications dabei", len(r["recent_modifications"]) > 0)
        check("last_modified gesetzt", r["last_modified"] is not None)

        # Mit scope
        r = vault_stats(scope="10_Life/tasks")
        check("scope-Filter: nur tasks", r["total_files"] == 3)

        # ─── Test 2: get_subgraph outgoing ───────────────────────────────────
        print("\n=== get_subgraph outgoing only ===")
        r = get_subgraph(start_path="10_Life/notes/alpha.md",
                         depth=2, include_incoming=False)
        node_paths = {n["path"] for n in r["nodes"]}
        check("alpha selbst drin",
              any("alpha.md" in p for p in node_paths))
        check("beta erreicht", any("beta.md" in p for p in node_paths))
        check("gamma erreicht (von alpha+beta)",
              any("gamma.md" in p for p in node_paths))
        # depth-Test: gamma sollte distance=1 haben (alpha→gamma direkt)
        gamma_node = next((n for n in r["nodes"] if "gamma.md" in n["path"]), None)
        check("gamma distance == 1", gamma_node and gamma_node["distance"] == 1)

        # ─── Test 3: get_subgraph mit incoming ───────────────────────────────
        print("\n=== get_subgraph mit incoming ===")
        r = get_subgraph(start_path="10_Life/notes/gamma.md",
                         depth=1, include_incoming=True)
        # gamma sollte alpha+beta als incoming haben
        node_paths = {n["path"] for n in r["nodes"]}
        check("gamma findet alpha (incoming)",
              any("alpha.md" in p for p in node_paths))
        check("gamma findet beta (incoming)",
              any("beta.md" in p for p in node_paths))
        # Edges-Type-Check
        in_edges = [e for e in r["edges"] if e["type"] == "incoming"]
        check("incoming-edges vorhanden", len(in_edges) > 0)

        # ─── Test 4: get_subgraph max_nodes-Truncation ───────────────────────
        print("\n=== get_subgraph max_nodes ===")
        r = get_subgraph(start_path="10_Life/notes/alpha.md",
                         depth=3, max_nodes=2, include_incoming=True)
        check("max_nodes=2 respektiert", r["total_nodes"] <= 2)
        check("truncated=True bei cap", r["truncated"] is True)

        # ─── Test 5: random_note ─────────────────────────────────────────────
        print("\n=== random_note ===")
        # Mehrere Aufrufe — sollen verschiedene Noten geben (mit hoher Wahrscheinlichkeit)
        # Aber auf jeden Fall: gueltige Note zurueck
        r = random_note(scope="10_Life/notes")
        check("random_note returnt path", r.get("path", "").endswith(".md"))
        check("random_note hat body_preview", "body_preview" in r)
        check("total_candidates > 0", r["total_candidates"] > 0)

        # tag_filter
        r = random_note(tag_filter="arbeit")
        if "path" in r:
            # alpha + gamma haben tag "arbeit", beta nicht
            check("tag_filter 'arbeit' findet alpha oder gamma",
                  "alpha" in r["path"] or "gamma" in r["path"],
                  f"got {r['path']}")

        # exclude_status
        r = random_note(scope="10_Life/notes", exclude_status=["deprecated"])
        if "path" in r:
            check("exclude_status filtert gamma raus",
                  "gamma" not in r["path"], f"got {r['path']}")

        # No candidates
        r = random_note(tag_filter="gibts-nicht-12345")
        check("no candidates returns error",
              r.get("error") == "no candidates" and r["total_candidates"] == 0)

        # ─── Test 6: file_audit ──────────────────────────────────────────────
        print("\n=== file_audit ===")
        # Audit-Log manuell schreiben (um nicht von Tool-Calls abhaengig zu sein)
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {"ts": "2026-05-07T10:00:00.000+00:00", "kind": "tool_call",
             "tool": "edit_file", "args": {"path": "10_Life/notes/alpha.md", "body": "neu"},
             "latency_ms": 12.3, "error": False, "result": {}},
            {"ts": "2026-05-08T11:00:00.000+00:00", "kind": "tool_call",
             "tool": "edit_file_replace", "args": {"path": "10_Life/notes/alpha.md",
                                                    "find": "x", "replace": "y"},
             "latency_ms": 8.5, "error": False, "result": {}},
            {"ts": "2026-05-08T12:00:00.000+00:00", "kind": "tool_call",
             "tool": "read_file", "args": {"path": "10_Life/notes/beta.md"},
             "latency_ms": 1.0, "error": False, "result": {}},
        ]
        with open(audit_log, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        r = file_audit(path="10_Life/notes/alpha.md")
        check("alpha-Events: 2", r["total"] == 2,
              f"got {r['total']}")
        # neueste zuerst
        check("Reihenfolge: 2026-05-08 vor 2026-05-07",
              r["events"][0]["ts"] > r["events"][1]["ts"])

        r = file_audit(path="10_Life/notes/alpha.md", since="2026-05-08")
        check("since-Filter: nur 1 Event seit 2026-05-08",
              r["total"] == 1, f"got {r['total']}")

        r = file_audit(path="10_Life/notes/nonexistent.md")
        check("nicht-matchender Path: 0 Events", r["total"] == 0)

        # ─── Test 7: project_overview ────────────────────────────────────────
        print("\n=== project_overview ===")
        r = project_overview(slug="alpha-proj")
        check("exists=True", r["exists"] is True)
        check("status=active", r["status"] == "active")
        check("counts.notes >= 1", r["counts"]["notes"] >= 1)
        check("counts.tasks_open = 2", r["counts"]["tasks_open"] == 2,
              f"got {r['counts']}")
        check("counts.tasks_done = 1", r["counts"]["tasks_done"] == 1)
        check("hours_total = 5,5",
              r.get("hours_total") == 5.5,
              f"got {r.get('hours_total')}")
        check("context_preview vorhanden",
              "Kontext-Text" in r.get("context_preview", ""))
        # tasks_open_top sortiert nach priority
        tot = r["tasks_open_top"]
        check("urgent task zuerst",
              tot[0]["priority"] == "urgent",
              f"got {tot[0]}")

        r = project_overview(slug="gibts-nicht")
        check("Nicht-existent: exists=False", r["exists"] is False)

        # ─── Test 8: Validator-Errors ────────────────────────────────────────
        print("\n=== Validator-Errors ===")
        try:
            get_subgraph(start_path="x.md", depth=10)
            check("depth=10 raises", False)
        except ToolError as e:
            check("depth>5 → ToolError", "depth" in str(e).lower())

        try:
            file_audit(path="x.md", since="bad-date")
            check("bad since raises", False)
        except ToolError as e:
            check("bad since → ToolError", "since" in str(e).lower())

        try:
            project_overview(slug="UPPERCASE-not-slug!")
            check("invalid slug raises", False)
        except ToolError as e:
            check("invalid slug → ToolError",
                  "slug" in str(e).lower())

    print("\n" + "=" * 50)
    if failures:
        print(f"FAIL: {failures} Tests fehlgeschlagen")
        return 1
    print("OK: alle Tests gruen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
