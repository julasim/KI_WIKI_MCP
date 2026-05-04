"""KI-OS MCP — Strikter Stress-Test gegen Production-Server.

Test-Kategorien:
  A. Auth & Security
  B. Path Traversal & Injection
  C. Tool Edge Cases (per Tool)
  D. Concurrency
  E. Lifecycle Integration
  F. Performance / Limits

Alle Test-Files mit Präfix `stress-test-` markiert.
Cleanup am Ende — Vault soll danach 100% sauber sein.
"""
import asyncio, json, sys, io, time, secrets, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import httpx
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession

cfg = json.load(open("C:/Users/juliu/.claude.json", encoding="utf-8"))
AUTH = cfg["mcpServers"]["ki-os-vault"]["headers"]["Authorization"]
TOKEN = AUTH.removeprefix("Bearer ")
URL = "https://76-13-10-79.sslip.io/mcp/"
HEALTH = "https://76-13-10-79.sslip.io/health"

# Test-Result-Aggregator
PASS, FAIL, WARN = [], [], []
CREATED_PATHS = set()  # für Cleanup


def ok(name, detail=""):
    PASS.append(f"  [PASS] {name}{(' — ' + detail) if detail else ''}")

def fail(name, detail):
    FAIL.append(f"  [FAIL] {name} — {detail}")

def warn(name, detail):
    WARN.append(f"  [WARN] {name} — {detail}")


def unwrap(res):
    return json.loads(res.content[0].text)


# ============================================================
# A. AUTH & SECURITY (httpx direkt, kein MCP-Client)
# ============================================================
async def cat_auth():
    print("\n=== A. AUTH & SECURITY (raw HTTPS) ===")
    init_payload = {"jsonrpc":"2.0","id":1,"method":"initialize",
                    "params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
    headers_base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    async with httpx.AsyncClient(verify=True, follow_redirects=False, timeout=10) as c:
        # A1: Health ohne Auth → 200
        r = await c.get(HEALTH)
        if r.status_code == 200 and r.json().get("status") == "ok":
            ok("A1 health no-auth", "200 OK")
        else:
            fail("A1 health no-auth", f"HTTP {r.status_code}")

        # A2: /mcp/ ohne Auth → 401
        r = await c.post(URL, headers=headers_base, json=init_payload)
        if r.status_code == 401:
            ok("A2 mcp no-auth → 401", r.json().get("error",""))
        else:
            fail("A2 mcp no-auth", f"HTTP {r.status_code} (expected 401)")

        async def safe_post(label, headers):
            try:
                r = await c.post(URL, headers=headers, json=init_payload)
                return r.status_code, r.text[:120]
            except Exception as e:
                return None, f"client-rejected: {type(e).__name__}: {str(e)[:80]}"

        # A3: /mcp/ leerer Bearer → 401 (oder client-blockiert)
        sc, body = await safe_post("A3", {**headers_base, "Authorization": "Bearer x"})
        # Echter "leerer" Bearer "Bearer " wird von httpx geblockt; nehmen "Bearer x"
        if sc == 401:
            ok("A3 invalid bearer 'x' → 401")
        else:
            fail("A3 invalid bearer", f"sc={sc} {body}")

        # A4: lowercase "bearer"
        sc, body = await safe_post("A4", {**headers_base, "Authorization": f"bearer {TOKEN}"})
        if sc == 401:
            ok("A4 lowercase 'bearer' → 401", "case-sensitive (good)")
        elif sc == 200:
            warn("A4 lowercase 'bearer' accepted", "server accepts non-canonical scheme")
        else:
            warn("A4 lowercase 'bearer'", f"sc={sc} {body}")

        # A5: x-api-key only
        sc, body = await safe_post("A5", {**headers_base, "x-api-key": TOKEN})
        if sc == 401:
            ok("A5 x-api-key only → 401", "Bearer required")
        else:
            warn("A5 x-api-key", f"sc={sc} {body}")

        # A6: Header injection — \r\n CRLF in token
        sc, body = await safe_post("A6", {**headers_base, "Authorization": f"Bearer {TOKEN}\r\nX-Admin: yes"})
        if sc is None:
            ok("A6 CRLF injection blocked", body)
        elif sc in (400, 401):
            ok(f"A6 CRLF injection → {sc}", "rejected")
        else:
            fail(f"A6 CRLF injection accepted", f"sc={sc}")

        # A7: trailing whitespace im Token
        sc, body = await safe_post("A7", {**headers_base, "Authorization": f"Bearer {TOKEN}  "})
        if sc == 200:
            ok("A7 trailing whitespace tolerated", "Server strip() greift")
        elif sc == 401:
            warn("A7 trailing whitespace rejected", "minor — Server kein strip()")
        else:
            warn(f"A7 ws", f"sc={sc} {body}")

        # A8: GET auf /mcp/ statt POST
        try:
            r = await c.get(URL, headers={**headers_base, "Authorization": AUTH})
            if r.status_code in (405, 400, 406, 200):
                ok(f"A8 GET on /mcp/ → {r.status_code}", "no crash")
            else:
                warn(f"A8 GET on /mcp/", f"HTTP {r.status_code}")
        except Exception as e:
            ok("A8 GET on /mcp/ failed cleanly", type(e).__name__)

        # A9: Massive Header (DoS-attempt) — 8KB Authorization (64KB lehnt httpx ab)
        h = {**headers_base, "Authorization": "Bearer " + ("x" * 8000)}
        try:
            r = await c.post(URL, headers=h, json=init_payload, timeout=5)
            if r.status_code in (400, 401, 413, 431):
                ok(f"A9 massive header → {r.status_code}", "rejected")
            else:
                warn(f"A9 massive header → {r.status_code}", "should reject")
        except Exception as e:
            ok("A9 massive header rejected", type(e).__name__)


# ============================================================
# B. PATH TRAVERSAL (über MCP-Tool-Calls)
# ============================================================
async def cat_path(s):
    print("\n=== B. PATH TRAVERSAL & INJECTION ===")
    payloads = [
        ("../etc/passwd", "rel ../"),
        ("../../etc/passwd", "rel ../../"),
        ("/etc/passwd", "absolute /etc"),
        ("..\\..\\windows\\system32\\drivers\\etc\\hosts", "windows backslash"),
        ("10_Life/../../etc/passwd", "mid-path .."),
        ("10_Life/daily/../../../etc/passwd", "deep .."),
        ("./10_Life/daily/2026-05-04.md", "leading ./"),
        ("10_Life//daily//2026-05-04.md", "double slash"),
    ]
    for path, label in payloads:
        d = unwrap(await s.call_tool("read_file", {"path": path}))
        if "error" in d:
            err = d["error"]
            # Path-traversal MUSS abgewiesen werden
            if "ausserhalb" in err.lower() or "nicht gefunden" in err.lower():
                ok(f"B path '{label}'", f"rejected: {err[:60]}")
            else:
                warn(f"B path '{label}'", f"unexpected error: {err}")
        else:
            # Hat Inhalt zurückgegeben → Sicherheitslücke!
            if "etc" in path or "windows" in path:
                fail(f"B path '{label}'", "RETURNED CONTENT — SECURITY HOLE")
            else:
                ok(f"B path '{label}'", "normalized + read OK")

    # Null-byte injection
    try:
        d = unwrap(await s.call_tool("read_file", {"path": "10_Life/daily/2026-05-04.md\x00.txt"}))
        if "error" in d:
            ok("B null-byte injection", "rejected")
        else:
            fail("B null-byte injection", "got content despite null byte")
    except Exception as e:
        ok("B null-byte injection raised", type(e).__name__)


# ============================================================
# C. TOOL EDGE CASES
# ============================================================
async def cat_tools(s):
    print("\n=== C. TOOL EDGE CASES ===")

    # === search_vault ===
    d = unwrap(await s.call_tool("search_vault", {"query": "[invalid("}))
    if "error" in d and "regex" in d["error"].lower():
        ok("C search invalid regex", "graceful error")
    else:
        warn("C search invalid regex", f"got {d}")

    d = unwrap(await s.call_tool("search_vault", {"query": "matura", "max_results": 1000}))
    ok(f"C search large max_results=1000", f"returned {d.get('total',0)}")

    d = unwrap(await s.call_tool("search_vault", {"query": "matura|abitur", "scope": "99_NonExistent"}))
    if "error" in d:
        ok("C search non-existent scope", d["error"][:50])
    else:
        warn("C search non-existent scope", "should error or return empty")

    # === read_file ===
    d = unwrap(await s.call_tool("read_file", {"path": "Y_DOES_NOT_EXIST.md"}))
    if "error" in d:
        ok("C read non-existent", d["error"][:50])
    else:
        fail("C read non-existent", "no error")

    # === list_files ===
    d = unwrap(await s.call_tool("list_files", {"path": "08_Templates"}))
    if "entries" in d:
        ok("C list_files 08_Templates", f"{len(d['entries'])} entries")
    else:
        warn("C list_files 08_Templates", str(d))

    d = unwrap(await s.call_tool("list_files", {"path": "FAKE_FOLDER_XYZ"}))
    if "error" in d:
        ok("C list_files non-existent", d["error"][:50])
    else:
        warn("C list_files non-existent", "no error")

    # === create_note slug edge cases ===
    d = unwrap(await s.call_tool("create_note", {"title": "x" * 80}))
    if "error" in d and "zu lang" in d.get("error",""):
        ok("C create_note slug too long", "rejected")
    else:
        warn("C create_note slug too long", str(d))

    d = unwrap(await s.call_tool("create_note", {"title": "@@@!!!"}))
    if d.get("path"):
        ok("C create_note pure-special-chars", f"became {d['path']}")
        CREATED_PATHS.add(d["path"])
    else:
        warn("C create_note pure-special-chars", str(d))

    # Duplicate detection
    d = unwrap(await s.call_tool("create_note", {"title": "stress-test-dup-marker"}))
    if "error" in d:
        warn("C create_note first attempt failed", d["error"])
    else:
        CREATED_PATHS.add(d["path"])
        d2 = unwrap(await s.call_tool("create_note", {"title": "stress-test-dup-marker"}))
        if "error" in d2 and "existiert" in d2.get("error","").lower():
            ok("C create_note duplicate detected", "rejected")
        else:
            fail("C create_note duplicate", "should have errored")

    # === create_task ===
    d = unwrap(await s.call_tool("create_task", {"title": "stress-test-bad-prio", "priority": "blocker"}))
    if "error" in d:
        ok("C create_task bad priority", d["error"][:50])
    else:
        warn("C create_task bad priority", "accepted invalid priority")
        CREATED_PATHS.add(d.get("path",""))

    d = unwrap(await s.call_tool("create_task", {"title": "stress-test-bad-recur", "recurrence": "daily-ish"}))
    if "error" in d:
        ok("C create_task bad recurrence", d["error"][:50])
    else:
        warn("C create_task bad recurrence", "accepted invalid recurrence")
        CREATED_PATHS.add(d.get("path",""))

    # === task() ===
    d = unwrap(await s.call_tool("task", {"id": "t-no-such-task-xyz", "action": "done"}))
    if "error" in d and "nicht gefunden" in d.get("error","").lower():
        ok("C task() unknown id", "rejected")
    else:
        fail("C task() unknown id", str(d))

    # snooze ohne snooze_until
    # Erst Test-Task anlegen
    d = unwrap(await s.call_tool("create_task", {"title": "stress-test-snooze-target", "priority": "low"}))
    snooze_target = d.get("path")
    snooze_id = d.get("id")
    if snooze_target:
        CREATED_PATHS.add(snooze_target)
        d = unwrap(await s.call_tool("task", {"id": snooze_id, "action": "snooze"}))
        if "error" in d and "snooze_until" in d.get("error",""):
            ok("C task snooze without snooze_until", "rejected")
        else:
            fail("C task snooze without snooze_until", str(d))

    # === create_meeting without attendees ===
    d = unwrap(await s.call_tool("create_meeting", {"title": "stress-test-no-attendees", "attendees": []}))
    if "error" in d and "attendees" in d.get("error","").lower():
        ok("C create_meeting no attendees", "rejected")
    else:
        warn("C create_meeting no attendees", "accepted empty list")
        if d.get("path"): CREATED_PATHS.add(d["path"])

    # === edit_file on non-existent ===
    d = unwrap(await s.call_tool("edit_file", {"path": "NOPE.md", "frontmatter_updates": {"x": 1}}))
    if "error" in d:
        ok("C edit_file non-existent", d["error"][:50])
    else:
        fail("C edit_file non-existent", "no error")

    # === append_to_daily empty section ===
    # Future-date daily creation
    d = unwrap(await s.call_tool("append_to_daily", {
        "text": "stress-test future daily marker",
        "date": "2099-12-31",
        "section": "TestSection"
    }))
    if d.get("path"):
        ok("C append future daily new section", f"created {d['path']}")
        CREATED_PATHS.add("10_Life/daily/2099-12-31.md")

    # === project_context non-existent project ===
    d = unwrap(await s.call_tool("project_context", {"project": "totally-fake-xyz", "text": "noop"}))
    if "error" in d:
        ok("C project_context non-existent", d["error"][:50])
    else:
        warn("C project_context non-existent", "created folder despite missing")


# ============================================================
# D. CONCURRENCY
# ============================================================
async def cat_concurrency(s):
    print("\n=== D. CONCURRENCY ===")

    # D1: 10 parallele list_tasks → alle gleich?
    coros = [s.call_tool("list_tasks", {"limit": 5}) for _ in range(10)]
    results = await asyncio.gather(*coros)
    parsed = [unwrap(r) for r in results]
    counts = set(p.get("total_matched", -1) for p in parsed)
    if len(counts) == 1:
        ok("D1 10x parallel list_tasks", f"all consistent: total={counts.pop()}")
    else:
        warn("D1 10x parallel list_tasks", f"inconsistent: {counts}")

    # D2: Gleicher create_note 5x parallel — nur 1 sollte ok sein
    title = f"stress-test-race-{int(time.time())}"
    coros = [s.call_tool("create_note", {"title": title}) for _ in range(5)]
    results = await asyncio.gather(*coros, return_exceptions=True)
    successes = []
    failures = []
    for r in results:
        if isinstance(r, Exception):
            failures.append(str(r)[:40])
            continue
        d = unwrap(r)
        if "error" in d:
            failures.append(d["error"][:40])
        else:
            successes.append(d["path"])
            CREATED_PATHS.add(d["path"])
    if len(successes) == 1 and len(failures) == 4:
        ok("D2 race condition guard", f"1 win, 4 dup-rejections")
    elif len(successes) > 1:
        fail("D2 race condition", f"{len(successes)} parallel writes succeeded — possible duplicate")
    else:
        warn("D2 race", f"successes={len(successes)} failures={len(failures)}")

    # D3: Pending-Delete-Tokens — multiple requests, wahllose Confirms
    test_paths = []
    for i in range(3):
        d = unwrap(await s.call_tool("create_note", {"title": f"stress-test-multidelete-{i}"}))
        if d.get("path"):
            test_paths.append(d["path"])
            CREATED_PATHS.add(d["path"])

    tokens = []
    for p in test_paths:
        d = unwrap(await s.call_tool("request_delete", {"path": p}))
        if d.get("confirm_token"):
            tokens.append((d["confirm_token"], p))

    if len(tokens) == 3:
        ok("D3 multiple pending deletes", f"{len(tokens)} tokens issued")
        # confirm in reverse order — should still work (token-based, not order-based)
        for token, path in reversed(tokens):
            d = unwrap(await s.call_tool("confirm_delete", {"token": token}))
            if d.get("deleted"):
                pass  # ok
            else:
                fail(f"D3 confirm reverse-order {path}", d.get("error",""))
                continue
            CREATED_PATHS.discard(path)
        ok("D3 confirm in reverse order", "all 3 deleted")

        # Confirm same token twice — second should fail
        d = unwrap(await s.call_tool("confirm_delete", {"token": tokens[0][0]}))
        if "error" in d:
            ok("D3 confirm token twice", "second rejected (token consumed)")
        else:
            fail("D3 confirm token twice", "deleted again somehow")


# ============================================================
# E. LIFECYCLE INTEGRATION
# ============================================================
async def cat_lifecycle(s):
    print("\n=== E. LIFECYCLE INTEGRATION ===")
    # Vollständiger Task-Lifecycle
    d = unwrap(await s.call_tool("create_task", {
        "title": "stress-test-lifecycle",
        "priority": "high",
        "context": "@stress",
        "recurrence": "weekly",
    }))
    if "error" in d:
        fail("E lifecycle create", d["error"]); return
    path = d["path"]; tid = d["id"]; CREATED_PATHS.add(path)

    # done → reopen → snooze → edit → check immer richtig
    states = [
        ("done", {"action":"done"}, lambda fm: fm["status"]=="done" and fm.get("last_completed")),
        ("reopen", {"action":"reopen"}, lambda fm: fm["status"]=="open" and "last_completed" not in fm),
        ("snooze", {"action":"snooze","snooze_until":"2099-01-01"}, lambda fm: fm["status"]=="snoozed" and fm.get("due")=="2099-01-01"),
        ("edit prio", {"action":"edit","priority":"low"}, lambda fm: fm["priority"]=="low"),
    ]
    all_pass = True
    for label, params, check in states:
        await s.call_tool("task", {"id": tid, **params})
        rd = unwrap(await s.call_tool("read_file", {"path": path}))
        fm = rd["frontmatter"]
        if check(fm):
            ok(f"E lifecycle {label}", "✓")
        else:
            fail(f"E lifecycle {label}", f"FM={fm}")
            all_pass = False

    # Move dieses Task zu neuer Slug
    new_path = path.replace("stress-test-lifecycle", "stress-test-lifecycle-renamed")
    d = unwrap(await s.call_tool("move", {"source": path, "dest": new_path, "dry_run": True}))
    if d.get("dry_run") and d.get("id_change"):
        ok("E lifecycle move dry_run", f"id {d['id_change']['old']} → {d['id_change']['new']}")
    d = unwrap(await s.call_tool("move", {"source": path, "dest": new_path}))
    if d.get("moved"):
        ok("E lifecycle move real", "moved")
        CREATED_PATHS.discard(path)
        CREATED_PATHS.add(new_path)
    else:
        fail("E lifecycle move", str(d))

    # Self-ref Wikilink-Test: 2 Notes die sich gegenseitig referenzieren
    a = unwrap(await s.call_tool("create_note", {
        "title": "stress-test-link-a",
        "body": "Verlinkt auf [[stress-test-link-b]] und [[stress-test-link-b|B-Display]] und [[stress-test-link-b#sec]]"
    }))
    b = unwrap(await s.call_tool("create_note", {
        "title": "stress-test-link-b",
        "body": "Verlinkt auf [[stress-test-link-a]]"
    }))
    if a.get("path"): CREATED_PATHS.add(a["path"])
    if b.get("path"): CREATED_PATHS.add(b["path"])

    # Move B → b-renamed: erwarte 3 Wikilink-Replacements in A
    new_b = b["path"].replace("stress-test-link-b", "stress-test-link-b-renamed")
    d = unwrap(await s.call_tool("move", {"source": b["path"], "dest": new_b}))
    if d.get("moved"):
        updated = d.get("wikilinks_updated", [])
        for u in updated:
            if u["path"] == a["path"]:
                if u["wikilink_replacements"] == 3:
                    ok("E wikilink mass-rewrite (3 forms)", "all 3 variants rewritten")
                else:
                    fail("E wikilink rewrite count", f"got {u['wikilink_replacements']} expected 3")
        # Verify A's body
        rd = unwrap(await s.call_tool("read_file", {"path": a["path"]}))
        body = rd["body"]
        if "stress-test-link-b-renamed" in body and "[[stress-test-link-b]]" not in body:
            ok("E wikilink read-back", "old gone, new present")
        else:
            fail("E wikilink read-back", body)
        CREATED_PATHS.discard(b["path"])
        CREATED_PATHS.add(new_b)


# ============================================================
# F. PERFORMANCE / LIMITS
# ============================================================
async def cat_perf(s):
    print("\n=== F. PERFORMANCE / LIMITS ===")
    # F1: search_vault über alles, kein scope
    t = time.perf_counter()
    d = unwrap(await s.call_tool("search_vault", {"query": "the|der|die|das|und|and", "max_results": 500}))
    dt = time.perf_counter() - t
    ok(f"F1 broad search", f"{d.get('total',0)} hits in {dt:.2f}s")

    # F2: Sehr großes append_to_daily
    big = "Stress-Test-Marker " * 1000  # ~20KB
    d = unwrap(await s.call_tool("append_to_daily", {
        "text": big, "date": "2099-12-31", "section": "TestSection"
    }))
    if d.get("path"):
        ok("F2 large append (20KB)", "OK")

    # F3: list_tasks limit=1
    d = unwrap(await s.call_tool("list_tasks", {"limit": 1}))
    if d.get("total_matched", 0) > 1 and len(d.get("tasks", [])) == 1:
        ok("F3 list_tasks limit=1", f"matched={d['total_matched']} returned=1")
    else:
        warn("F3 list_tasks limit=1", str(d)[:80])


# ============================================================
# CLEANUP
# ============================================================
async def cleanup(s):
    print("\n=== CLEANUP ===")
    cleaned = 0
    failed = []
    for path in sorted(CREATED_PATHS):
        d = unwrap(await s.call_tool("request_delete", {"path": path, "reason": "stress test cleanup"}))
        if "error" in d:
            failed.append((path, d["error"]))
            continue
        token = d["confirm_token"]
        d = unwrap(await s.call_tool("confirm_delete", {"token": token}))
        if d.get("deleted"):
            cleaned += 1
        else:
            failed.append((path, d.get("error","")))

    # Future daily
    for path in ["10_Life/daily/2099-12-31.md"]:
        d = unwrap(await s.call_tool("read_file", {"path": path}))
        if "error" not in d:
            rd = unwrap(await s.call_tool("request_delete", {"path": path}))
            if rd.get("confirm_token"):
                unwrap(await s.call_tool("confirm_delete", {"token": rd["confirm_token"]}))
                cleaned += 1

    print(f"  cleaned: {cleaned}")
    for p, err in failed:
        print(f"  FAILED: {p}: {err}")


# ============================================================
# MAIN
# ============================================================
async def main():
    # Auth-Tests vor Connect
    await cat_auth()

    # Mit Auth verbinden für Rest
    async with streamablehttp_client(URL, headers={"Authorization": AUTH}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print(f"\n[INIT] {len(tools.tools)} Tools live")
            assert len(tools.tools) == 15

            await cat_path(s)
            await cat_tools(s)
            await cat_concurrency(s)
            await cat_lifecycle(s)
            await cat_perf(s)
            await cleanup(s)

    # ===== BERICHT =====
    print("\n" + "="*60)
    print("STRESS-TEST-BERICHT")
    print("="*60)
    print(f"\n[PASS] {len(PASS)} Tests")
    for p in PASS: print(p)
    if WARN:
        print(f"\n[WARN] {len(WARN)} Tests")
        for w in WARN: print(w)
    if FAIL:
        print(f"\n[FAIL] {len(FAIL)} Tests")
        for f in FAIL: print(f)
    else:
        print("\n[FAIL] 0 Tests — alle Hard-Asserts gehalten")
    print("\n" + "="*60)
    print(f"GESAMT: {len(PASS)} pass, {len(WARN)} warn, {len(FAIL)} fail")
    print("="*60)


asyncio.run(main())
