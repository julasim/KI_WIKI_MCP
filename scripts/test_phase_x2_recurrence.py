"""Unit-Test fuer maintain.is_recurrence_due.

Pattern-Logik 1:1 aus Bot.ki_wiki_bot._is_recurrence_due. Wenn die hier
abweicht von Bot, gibt es nach Phase X3 silent drift bei recurring Tasks.

Run: python scripts/test_phase_x2_recurrence.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

sys.path.insert(0, "ki_os_mcp")
# Direct import - umgeht __init__.py side effects
import importlib.util
spec = importlib.util.spec_from_file_location(
    "maintain_test", "ki_os_mcp/maintain.py"
)
mod = importlib.util.module_from_spec(spec)
# Stub vault + audit so module can load standalone
import types
vault_stub = types.ModuleType("ki_os_mcp.vault")
vault_stub.VAULT_PATH = type("P", (), {"__truediv__": lambda s, x: vault_stub})()
audit_stub = types.ModuleType("ki_os_mcp.audit")
audit_stub.log_event = lambda *a, **k: None
sys.modules["ki_os_mcp"] = types.ModuleType("ki_os_mcp")
sys.modules["ki_os_mcp.vault"] = vault_stub
sys.modules["ki_os_mcp.audit"] = audit_stub
spec.loader.exec_module(mod)

is_recurrence_due = mod.is_recurrence_due


def assert_eq(label, got, expected):
    if got == expected:
        print(f"  PASS  {label}")
        return True
    print(f"  FAIL  {label} — expected {expected!r}, got {got!r}")
    return False


def main() -> int:
    failures = 0

    # --- daily ---
    today = date(2026, 5, 6)  # Mittwoch
    failures += not assert_eq(
        "daily: gestern done → fällig",
        is_recurrence_due("daily", "2026-05-05", today),
        True,
    )
    failures += not assert_eq(
        "daily: heute schon done → nicht fällig",
        is_recurrence_due("daily", "2026-05-06", today),
        False,
    )
    failures += not assert_eq(
        "daily: morgen done (defensiv) → nicht fällig",
        is_recurrence_due("daily", "2026-05-07", today),
        False,
    )
    failures += not assert_eq(
        "daily: kein last_completed → nicht reaktivieren",
        is_recurrence_due("daily", None, today),
        False,
    )
    failures += not assert_eq(
        "daily: vor 1 Woche → fällig (jeden Tag)",
        is_recurrence_due("daily", "2026-04-29", today),
        True,
    )

    # --- weekdays ---
    monday = date(2026, 5, 4)
    saturday = date(2026, 5, 9)
    sunday = date(2026, 5, 10)
    failures += not assert_eq(
        "weekdays: Montag, gestern done → fällig",
        is_recurrence_due("weekdays", "2026-05-03", monday),
        True,
    )
    failures += not assert_eq(
        "weekdays: Samstag → nicht fällig",
        is_recurrence_due("weekdays", "2026-05-08", saturday),
        False,
    )
    failures += not assert_eq(
        "weekdays: Sonntag → nicht fällig",
        is_recurrence_due("weekdays", "2026-05-08", sunday),
        False,
    )

    # --- weekly ---
    failures += not assert_eq(
        "weekly: vor 7 Tagen done → fällig",
        is_recurrence_due("weekly", "2026-04-29", today),
        True,
    )
    failures += not assert_eq(
        "weekly: vor 6 Tagen done → noch nicht",
        is_recurrence_due("weekly", "2026-04-30", today),
        False,
    )
    failures += not assert_eq(
        "weekly: vor 30 Tagen done → fällig",
        is_recurrence_due("weekly", "2026-04-06", today),
        True,
    )

    # --- monthly ---
    # last=2026-04-15, today=2026-05-15 → gleich Tag, anderer Monat → fällig
    failures += not assert_eq(
        "monthly: gleicher Tag im Folgemonat → fällig",
        is_recurrence_due("monthly", "2026-04-15", date(2026, 5, 15)),
        True,
    )
    # last=2026-04-15, today=2026-05-10 → noch nicht 15. erreicht
    failures += not assert_eq(
        "monthly: noch vor target_day → nicht fällig",
        is_recurrence_due("monthly", "2026-04-15", date(2026, 5, 10)),
        False,
    )
    # last=2026-04-15, today=2026-04-30 → gleicher Monat
    failures += not assert_eq(
        "monthly: gleicher Monat → nicht fällig",
        is_recurrence_due("monthly", "2026-04-15", date(2026, 4, 30)),
        False,
    )
    # last=2026-01-31, today=2026-02-28 → 28 ist letzter Feb-Tag, target_day=min(31,28)=28
    failures += not assert_eq(
        "monthly: 31er-Task im Februar → fällig am 28.",
        is_recurrence_due("monthly", "2026-01-31", date(2026, 2, 28)),
        True,
    )
    # last=2026-01-31, today=2026-02-27 → noch nicht 28
    failures += not assert_eq(
        "monthly: 31er-Task am 27.Feb → noch nicht",
        is_recurrence_due("monthly", "2026-01-31", date(2026, 2, 27)),
        False,
    )
    # Jahres-Wechsel: last=2025-12-15, today=2026-01-15
    failures += not assert_eq(
        "monthly: über Jahres-Wechsel → fällig",
        is_recurrence_due("monthly", "2025-12-15", date(2026, 1, 15)),
        True,
    )

    # --- date-Objekt als input (PyYAML kann unquoted ISO als date parsen) ---
    failures += not assert_eq(
        "daily: date-Objekt als last_completed",
        is_recurrence_due("daily", date(2026, 5, 5), today),
        True,
    )

    # --- unbekanntes Pattern ---
    failures += not assert_eq(
        "unknown pattern → False",
        is_recurrence_due("yearly", "2025-05-06", today),
        False,
    )

    print()
    if failures:
        print(f"FAIL: {failures} assertion(s) failed.")
        return 1
    print("ALL RECURRENCE-TESTS OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
