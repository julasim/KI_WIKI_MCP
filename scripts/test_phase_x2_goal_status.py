"""Smoke-Test fuer step_goal_status_check — verifiziert die Drift-Schwellen
und Habits/Sport-Score-Logik auf synthetischen Vault-Files.

Run: PYTHONIOENCODING=utf-8 python scripts/test_phase_x2_goal_status.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    failures = 0
    today = date.today()

    with tempfile.TemporaryDirectory() as tmp:
        vault_path = Path(tmp) / "vault"
        goals = vault_path / "10_Life" / "goals" / "5y-2031"

        # readme.md mit Drift-Ankern
        weekly_recent = (today - timedelta(days=5)).isoformat()
        monthly_old = (today - timedelta(days=90)).isoformat()
        readme = f"""# 5y-2031

**Letzter Wochen-Anker:** {weekly_recent}
**Letzter Monats-Anker:** {monthly_old}
**Letzter Quartals-Anker:** —
"""
        write(goals / "readme.md", readme)

        # habits.md: 7 Tage, 5 davon mit Häkchen
        habits_lines = ["| Datum | Sport | Lesen | Schlaf | Bildschirm | Vision | Wasser |",
                        "|---|---|---|---|---|---|---|"]
        for i in range(7):
            d = (today - timedelta(days=i)).isoformat()
            # Tag 0-4: viele ✓, Tag 5-6: wenig
            cells = ["✓"] * 5 + ["✗"] if i < 5 else ["✗"] * 6
            habits_lines.append(f"| {d} | " + " | ".join(cells) + " |")
        write(goals / "tracker" / "habits.md", "\n".join(habits_lines) + "\n")

        # sport-log.md: 4 Sessions in den letzten 7d, 8 in 30d
        sport_lines = []
        for i in range(4):
            d = (today - timedelta(days=i)).isoformat()
            sport_lines.append(f"| {d} | cardio | 30 | Run |")
        for i in range(7, 15):
            d = (today - timedelta(days=i)).isoformat()
            sport_lines.append(f"| {d} | kraft | 60 | Gym |")
        write(goals / "tracker" / "sport-log.md", "\n".join(sport_lines) + "\n")

        # ENV setzen + maintain-Modul mit gestubbtem vault laden
        os.environ["VAULT_PATH"] = str(vault_path)
        os.environ["MCP_TIMEZONE"] = "UTC"  # consistent mit date.today() in Tests

        # Module laden mit echtem vault.py + frontmatter-Stub
        import importlib.util
        import types

        # vault.py LADEN (echt) — braucht VAULT_PATH ENV
        v_spec = importlib.util.spec_from_file_location(
            "ki_os_mcp.vault", "ki_os_mcp/vault.py"
        )
        v_mod = importlib.util.module_from_spec(v_spec)

        # Stub-Pakete
        pkg = types.ModuleType("ki_os_mcp")
        sys.modules["ki_os_mcp"] = pkg
        sys.modules["ki_os_mcp.vault"] = v_mod
        v_spec.loader.exec_module(v_mod)
        pkg.vault = v_mod

        audit_stub = types.ModuleType("ki_os_mcp.audit")
        audit_stub.log_event = lambda *a, **k: None
        sys.modules["ki_os_mcp.audit"] = audit_stub
        pkg.audit = audit_stub

        # maintain.py laden
        m_spec = importlib.util.spec_from_file_location(
            "ki_os_mcp.maintain", "ki_os_mcp/maintain.py"
        )
        m_mod = importlib.util.module_from_spec(m_spec)
        sys.modules["ki_os_mcp.maintain"] = m_mod
        m_spec.loader.exec_module(m_mod)

        # Test step_goal_status_check
        result = m_mod.step_goal_status_check()
        print("Result:")
        import json
        print(json.dumps(result, indent=2, default=str))
        print()

        # Assertions
        def check(label, cond):
            nonlocal failures
            if cond:
                print(f"  PASS  {label}")
            else:
                print(f"  FAIL  {label}")
                failures += 1

        check("drift.weekly age=5 → ok", result["drift"]["weekly"]["status"] == "ok")
        check("drift.weekly age_days=5", result["drift"]["weekly"]["age_days"] == 5)
        check("drift.monthly age=90 → warn (>60)", result["drift"]["monthly"]["status"] == "warn")
        check("drift.quarterly missing → warn", result["drift"]["quarterly"]["status"] == "warn")
        # 25✓ / 42 possible = 59% → warn (Soll: 80%)
        check("habits 7d: 25/42 → 59% → warn", result["habits_7d"]["pct"] == 59)
        check("habits 7d status warn", result["habits_7d"]["status"] == "warn")
        # d>=cutoff_7 schliesst auch today-7 ein (Bot-Konsistenz, off-by-one bewusst übernommen)
        check("sport.d7=5 (inkl today-7)", result["sport"]["d7"] == 5)
        check("sport.d7>=3 → ok", result["sport"]["status"] == "ok")
        check("overall=warn (monthly drift)", result["overall"] == "warn")

    print()
    if failures:
        print(f"FAIL: {failures} assertion(s) failed.")
        return 1
    print("ALL GOAL-STATUS TESTS OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
