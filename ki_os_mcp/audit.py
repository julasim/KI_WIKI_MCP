"""Append-only Audit-Log für alle Tool-Calls.

Jede Tool-Invocation wird als JSON-Line geloggt:
  - timestamp, tool, args (sensitive masked), latency_ms, result_status

Auth-Events (success/fail) werden separat geloggt.

Log-Pfad ist konfigurierbar via env MCP_AUDIT_LOG (default /var/log/mcp/audit.log).
Verzeichnis wird beim Start angelegt. Format: JSONL, eine Zeile pro Event.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ki-os-mcp.audit")

AUDIT_LOG_PATH = Path(os.environ.get("MCP_AUDIT_LOG", "/var/log/mcp/audit.log"))

# Welche Argument-Keys sollen in Logs maskiert werden (Privacy/Secrets)
SENSITIVE_KEYS = {"token", "confirm_token", "password", "secret", "api_key"}

# Welche Tool-Args sollen NIE geloggt werden (zu groß / unwichtig)
TRUNCATE_KEYS = {"body", "text"}  # nur ersten 100 chars
TRUNCATE_LIMIT = 100

_lock = threading.Lock()


def _mask_args(args: dict[str, Any]) -> dict[str, Any]:
    """Maskiert sensitive Werte und kürzt sehr große Strings."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k in SENSITIVE_KEYS:
            out[k] = "***REDACTED***"
        elif k in TRUNCATE_KEYS and isinstance(v, str) and len(v) > TRUNCATE_LIMIT:
            out[k] = v[:TRUNCATE_LIMIT] + f"...<+{len(v) - TRUNCATE_LIMIT}ch>"
        else:
            out[k] = v
    return out


def _write(event: dict[str, Any]) -> None:
    """Append eine JSON-Line, thread-safe."""
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        with _lock:
            with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:
        log.error("Audit log write failed: %s", e)


def log_tool_call(
    tool: str,
    args: dict[str, Any],
    result: Any,
    latency_ms: float,
    error: bool = False,
) -> None:
    """Logge einen Tool-Call. Result wird auf Größe limitiert."""
    result_summary: dict[str, Any]
    if isinstance(result, dict):
        # Kurz-Summary: keys + ob error
        result_summary = {"keys": sorted(result.keys())[:10]}
        if "error" in result:
            result_summary["error"] = str(result["error"])[:200]
            error = True
    else:
        result_summary = {"type": type(result).__name__}

    _write(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": "tool_call",
            "tool": tool,
            "args": _mask_args(args),
            "latency_ms": round(latency_ms, 2),
            "error": error,
            "result": result_summary,
        }
    )


def log_auth(
    success: bool,
    client_ip: str | None,
    reason: str = "",
    user_agent: str | None = None,
) -> None:
    """Logge Auth-Events (success oder fail)."""
    _write(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": "auth",
            "success": success,
            "client_ip": client_ip,
            "user_agent": user_agent,
            "reason": reason,
        }
    )


def log_event(kind: str, **fields: Any) -> None:
    """Generischer Event-Logger (Server-Start, Token-Rotation, etc.)."""
    _write(
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": kind,
            **fields,
        }
    )


def time_call(fn):
    """Sync-Decorator: misst Latency, loggt Tool-Call.

    Tool-Funktionen haben Signature `(**kwargs) -> dict`. Erste Argument-Werte
    sind alle Tool-Parameter. Logging erfolgt nach Returns oder Exception.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        # Tool-Funktionen verwenden nur kwargs in unserem Code
        log_args = dict(kwargs)
        try:
            result = fn(*args, **kwargs)
            latency = (time.perf_counter() - start) * 1000
            log_tool_call(fn.__name__, log_args, result, latency)
            return result
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            log_tool_call(
                fn.__name__,
                log_args,
                {"error": f"{type(e).__name__}: {e}"},
                latency,
                error=True,
            )
            raise

    return wrapper
