"""Standalone-Test für die Phase-X1-Parser.

Benutzt synthetische Sample-Inputs die die Format-Annahmen aus
dashboard/lib/vault.ts spiegeln. Wenn die Parser hier rausfallen, fällt
der MCP-Server bei echten Vault-Daten genauso raus.

Run: python scripts/test_phase_x1_parsers.py
"""

from __future__ import annotations

import re
import sys

# ---------- Parser-Logik aus server.py kopiert (drop-in) ---------------------


def parse_vision(raw: str) -> dict:
    m = re.search(r">\s*\*\*([\s\S]+?)\*\*", raw)
    if not m:
        return {"text": "", "found": False}
    text = re.sub(r"\s+", " ", re.sub(r"\n>\s*", " ", m.group(1))).strip()
    return {"text": text, "found": True}


def parse_saeulen(raw: str) -> list:
    m = re.search(
        r"\|\s*Säule\s*\|\s*Status\s*\|\s*Nächster Anker\s*\|[^\n]*\n((?:\|[^\n]*\|\n)+)",
        raw,
    )
    if not m:
        return []
    out = []
    for row in m.group(1).strip().split("\n"):
        cells = [c.strip() for c in row.split("|")[1:-1]]
        if len(cells) < 2 or all(re.fullmatch(r"-+", c or "") for c in cells):
            continue
        out.append({
            "slug": cells[0].lower(),
            "label": cells[0],
            "kpi": cells[1] if len(cells) > 1 else "",
            "note": cells[2] if len(cells) > 2 else "",
        })
    return out


def parse_drift(raw: str) -> dict:
    out = {}
    for label, key in (
        ("Letzter Wochen-Anker", "weekly"),
        ("Letzter Monats-Anker", "monthly"),
        ("Letzter Quartals-Anker", "quarterly"),
    ):
        m = re.search(rf"\*\*{label}:\*\*\s*([\d-]+|—)", raw)
        if m:
            out[key] = m.group(1)
    return out


HABIT_KEYS = ["sport", "lesen", "schlaf", "bildschirm", "vision", "wasser"]


def parse_habits(raw: str) -> dict:
    rows = {}
    for line in raw.split("\n"):
        m = re.match(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|(.+)\|$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        values = {}
        for i, k in enumerate(HABIT_KEYS):
            c = cells[i] if i < len(cells) else ""
            values[k] = "ok" if c == "✓" else "bad" if c == "✗" else "skip"
        rows[m.group(1)] = values
    return rows


def parse_sport(raw: str) -> list:
    out = []
    for line in raw.split("\n"):
        m = re.match(
            r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(cardio|kraft)\s*\|\s*(\d+)\s*\|\s*(.*?)\s*\|$",
            line,
        )
        if m:
            out.append({"date": m.group(1), "art": m.group(2),
                        "dauer": int(m.group(3)), "notiz": m.group(4)})
    return out


def parse_wins(raw: str) -> list:
    out = []
    current_date = None
    for line in raw.split("\n"):
        dm = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", line)
        if dm:
            current_date = dm.group(1)
            continue
        if not current_date:
            continue
        bm = re.match(r"^-\s+(.+)", line)
        if bm:
            out.append({"date": current_date, "text": bm.group(1).strip()})
    return out


# ---------- Test-Cases -------------------------------------------------------


def assert_eq(label: str, got, expected) -> bool:
    if got == expected:
        print(f"  PASS  {label}")
        return True
    print(f"  FAIL  {label}\n         expected: {expected!r}\n         got:      {got!r}")
    return False


def main() -> int:
    failures = 0

    # --- Vision ---
    vision_raw = """
# Vision 2031

> **Ich bin Unternehmer, Familienmensch und körperlich präsent.
> Mein Personal-OS gibt mir Klarheit jeden Tag.**

Mehr Text danach.
"""
    got = parse_vision(vision_raw)
    failures += not assert_eq(
        "vision: extrahiert Manifesto",
        got["text"],
        "Ich bin Unternehmer, Familienmensch und körperlich präsent. Mein Personal-OS gibt mir Klarheit jeden Tag.",
    )
    failures += not assert_eq("vision: found=True", got["found"], True)

    failures += not assert_eq(
        "vision: leer wenn kein Manifesto",
        parse_vision("# Nur Text\n").get("found"),
        False,
    )

    # --- Säulen ---
    saeulen_raw = """
## Status

| Säule | Status | Nächster Anker | KPI |
|---|---|---|---|
| Gesundheit | OK | Sport 3×/Wo | trainings/woche |
| Beruf | warn | Q3-Plan | EUR/Monat |
"""
    got = parse_saeulen(saeulen_raw)
    failures += not assert_eq("saeulen: 2 Zeilen", len(got), 2)
    failures += not assert_eq("saeulen[0].label", got[0]["label"], "Gesundheit")
    failures += not assert_eq("saeulen[1].kpi", got[1]["kpi"], "warn")

    # --- Drift ---
    drift_raw = """
**Letzter Wochen-Anker:** 2026-05-04
**Letzter Monats-Anker:** 2026-04-30
**Letzter Quartals-Anker:** —
"""
    got = parse_drift(drift_raw)
    failures += not assert_eq("drift.weekly", got.get("weekly"), "2026-05-04")
    failures += not assert_eq("drift.monthly", got.get("monthly"), "2026-04-30")
    failures += not assert_eq("drift.quarterly", got.get("quarterly"), "—")

    # --- Habits ---
    habits_raw = """
| Datum | Sport | Lesen | Schlaf | Bildschirm | Vision | Wasser |
|---|---|---|---|---|---|---|
| 2026-05-04 | ✓ | ✓ | ✗ | ✓ | - | ✓ |
| 2026-05-03 | ✗ | - | ✓ | ✓ | ✓ | ✓ |
"""
    got = parse_habits(habits_raw)
    failures += not assert_eq("habits: 2 Tage geparst", len(got), 2)
    failures += not assert_eq(
        "habits[2026-05-04].sport=ok",
        got["2026-05-04"]["sport"],
        "ok",
    )
    failures += not assert_eq(
        "habits[2026-05-04].schlaf=bad",
        got["2026-05-04"]["schlaf"],
        "bad",
    )
    failures += not assert_eq(
        "habits[2026-05-04].vision=skip",
        got["2026-05-04"]["vision"],
        "skip",
    )

    # --- Sport ---
    sport_raw = """
| 2026-05-04 | cardio | 45 | Laufen Donau |
| 2026-05-03 | kraft | 60 | Pull/Push |
| Header-Zeile soll skippen | nicht | matchen | xx |
"""
    got = parse_sport(sport_raw)
    failures += not assert_eq("sport: 2 Sessions", len(got), 2)
    failures += not assert_eq("sport[0].art", got[0]["art"], "cardio")
    failures += not assert_eq("sport[0].dauer=45", got[0]["dauer"], 45)
    failures += not assert_eq("sport[1].notiz", got[1]["notiz"], "Pull/Push")

    # --- Wins ---
    wins_raw = """
## 2026-05-04
- Erste 5K geknackt
- 2× tief gearbeitet ohne Phone

## 2026-05-03
- Bot-Voice 100% Erkennung
"""
    got = parse_wins(wins_raw)
    failures += not assert_eq("wins: 3 Wins", len(got), 3)
    failures += not assert_eq("wins[0].date", got[0]["date"], "2026-05-04")
    failures += not assert_eq(
        "wins[0].text",
        got[0]["text"],
        "Erste 5K geknackt",
    )
    failures += not assert_eq("wins[2].date", got[2]["date"], "2026-05-03")

    print()
    if failures:
        print(f"FAIL: {failures} assertion(s) failed.")
        return 1
    print("ALL PARSERS OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
