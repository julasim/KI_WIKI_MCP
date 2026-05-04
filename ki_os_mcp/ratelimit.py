"""Token-Bucket Rate-Limiting per Client-IP.

Default: 60 requests/min/IP (sliding window via simple bucket).
Configurable via env MCP_RATE_LIMIT_PER_MIN (0 = disabled).

Implementation: in-memory dict, thread-safe via lock. Pro IP ein Bucket
mit `capacity` Tokens, refill `capacity / 60` Tokens pro Sekunde.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("ki-os-mcp.ratelimit")

CAPACITY = int(os.environ.get("MCP_RATE_LIMIT_PER_MIN", "60"))
DISABLED = CAPACITY <= 0
REFILL_PER_SEC = CAPACITY / 60.0 if CAPACITY > 0 else 0


@dataclass
class Bucket:
    tokens: float = float(CAPACITY)
    last_refill: float = field(default_factory=time.monotonic)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-Bucket per Client-IP. Liefert HTTP 429 bei Überschreitung."""

    def __init__(self, app):
        super().__init__(app)
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()

    def _client_ip(self, request: Request) -> str:
        # Hinter Caddy: X-Forwarded-For honoren (Caddy setzt das)
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _take(self, ip: str) -> tuple[bool, float]:
        """Versuche, 1 Token zu nehmen. Returns (allowed, retry_after_sec)."""
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(ip)
            if b is None:
                b = Bucket()
                self._buckets[ip] = b
            # Refill
            elapsed = now - b.last_refill
            b.tokens = min(CAPACITY, b.tokens + elapsed * REFILL_PER_SEC)
            b.last_refill = now
            if b.tokens >= 1:
                b.tokens -= 1
                return True, 0.0
            # Wieviele Sekunden bis nächster Token?
            need = 1 - b.tokens
            return False, need / REFILL_PER_SEC if REFILL_PER_SEC > 0 else 60.0

    async def dispatch(self, request: Request, call_next):
        if DISABLED:
            return await call_next(request)
        # Health-Endpoint nicht limitieren (Healthchecks)
        if request.url.path == "/health":
            return await call_next(request)
        ip = self._client_ip(request)
        allowed, retry_after = self._take(ip)
        if not allowed:
            log.warning("Rate-limit hit: ip=%s retry_after=%.1fs", ip, retry_after)
            return JSONResponse(
                {"error": "rate_limit_exceeded", "retry_after": round(retry_after, 1)},
                status_code=429,
                headers={"Retry-After": str(int(retry_after) + 1)},
            )
        return await call_next(request)
