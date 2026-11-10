"""HTTP-mode abuse guards and boot-readiness gate (ASGI middleware).

Server-side controls for the public deployment (README.md "이용 제한과 문제 해결"):
health endpoints, kill switch, per-IP token bucket keyed on CF-Connecting-IP,
boot-readiness gate, request-body cap and a global concurrency limit. Query
length (500자) is enforced at the tool layer, not here.

Order per request:
  health (GET /livez|/readyz) → kill switch → rate limit →
  readiness gate (MCP path only) → (POST only) body cap → concurrency slot

Why health comes first, unconditionally. The container health check and the
cutover script must be able to tell "app is up and deliberately paused" from
"app is gone", so the health paths keep answering while the kill switch is set.
They cannot be restricted by source: a request arriving through the published
port reaches the container as the bridge gateway address, so loopback is
indistinguishable from the internet (UPDATE-PLAN-R1 §1). The tunnel forwards
every path to this origin, so both paths are public — they are therefore
fixed-shape O(1) answers read from memory that touch no index, database or
credential, and the failure stage, exception text and per-stage timings stay in
the container-local diagnostics file (see readiness.STATE_FILE).

Why the readiness gate is scoped to the MCP path. Gating every path would turn
an ordinary 404 into a 503 during warm-up, which changes routing semantics and
leaks state on paths that have nothing to do with serving.

Rejections answer directly with a small JSON body and never reach the MCP app.
Every response this middleware produces carries X-KRND-Release (which release
answered) and X-KRND-State (which control answered), so an external probe can
distinguish an app 503 from an edge 502 during a cutover. Responses produced
outside this middleware (FastMCP's host guard, Starlette's error handler) are
not wrapped and carry neither.
"""

import asyncio
import json
import os
import time

from . import readiness

# FastMCP's streamable-http route (fastmcp.settings.streamable_http_path); the
# server never overrides it. Matched after stripping a trailing slash.
MCP_PATH = "/mcp"
HEALTH_PATHS = ("/livez", "/readyz")


def guard_config_from_env() -> dict:
    return {
        "kill_switch_path": os.environ.get(
            "RND_FACTLINK_KILL_SWITCH", ""),
        "max_body": int(os.environ.get("RND_FACTLINK_MAX_BODY", "65536")),
        "rate_capacity": float(os.environ.get("RND_FACTLINK_RATE_CAPACITY", "10")),
        "rate_refill_per_s": float(os.environ.get("RND_FACTLINK_RATE_REFILL", "0.5")),
        "max_concurrency": int(os.environ.get("RND_FACTLINK_MAX_CONCURRENCY", "4")),
    }


class AbuseGuard:
    def __init__(self, app, *, kill_switch_path: str, max_body: int,
                 rate_capacity: float, rate_refill_per_s: float,
                 max_concurrency: int, status_fn=None, release: str | None = None):
        self.app = app
        self.kill_switch_path = kill_switch_path
        self.max_body = max_body
        self.rate_capacity = rate_capacity
        self.rate_refill_per_s = rate_refill_per_s
        # single injection point so the gate and /readyz can never disagree
        # about the state (tests drive both through this one callable)
        self.status_fn = status_fn or readiness.status
        self.release = release if release is not None else readiness.release_id()
        self._buckets: dict[str, list[float]] = {}  # ip -> [tokens, last_refill_ts]
        self._sem = asyncio.Semaphore(max_concurrency)
        self._prune_task: asyncio.Task | None = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # exactly one optional trailing slash, matching what the router accepts —
        # rstrip("/") would have let "/readyz////" through the health bypass
        raw_path = scope.get("path") or "/"
        path = raw_path[:-1] if len(raw_path) > 1 and raw_path.endswith("/") else raw_path
        if scope.get("method") in ("GET", "HEAD") and path in HEALTH_PATHS:
            # HEAD is what several monitors probe with; answering it 404 from the
            # router (and charging a rate token for it) would look like an outage
            return await self._health(send, path,
                                      with_body=scope.get("method") == "GET")

        if self._prune_task is None:
            # started lazily because no event loop exists at construction time
            self._prune_task = asyncio.get_running_loop().create_task(self._prune_loop())

        if self.kill_switch_path and os.path.exists(self.kill_switch_path):
            return await self._reject(send, 503, "killed",
                                      "서비스가 일시 중지되었습니다", retry_after=30)

        if not self._take_token(self._client_ip(scope)):
            return await self._reject(send, 429, "rate_limited",
                                      "요청 한도 초과 — 잠시 후 재시도하십시오", retry_after=10)

        if path == MCP_PATH and self.status_fn() not in readiness.SERVING_STATUSES:
            return await self._reject(send, 503, "not_ready",
                                      "서버 준비 중입니다 — 잠시 후 재시도하십시오", retry_after=5)

        if scope.get("method") != "POST":
            return await self.app(scope, receive, self._send_with_release(send))

        length = self._content_length(scope)
        if length is None or length > self.max_body:
            return await self._reject(send, 413, "body_too_large",
                                      "요청 본문이 허용 크기를 초과합니다")

        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=5.0)
        except asyncio.TimeoutError:
            return await self._reject(send, 503, "busy",
                                      "동시 처리 한도 초과 — 잠시 후 재시도하십시오",
                                      retry_after=5)
        try:
            await self.app(scope, receive, self._send_with_release(send))
        finally:
            self._sem.release()

    def _send_with_release(self, send):
        """Tag downstream responses with the release that produced them."""
        async def wrapped(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-krnd-release", self.release.encode("ascii", "replace"))]
            await send(message)
        return wrapped

    async def _health(self, send, path: str, with_body: bool = True) -> None:
        """Fixed-shape liveness/readiness. Deliberately minimal: status and the
        opaque release only. `degraded` and `failed` serve traffic (bounded
        fail-open) but never answer 200 — the container health check and the
        cutover script must both see them as not-ready."""
        if path == "/livez":
            return await self._respond(send, 200, "live", {"status": "live"},
                                       with_body=with_body)
        status = self.status_fn()
        code = 200 if status == readiness.STATUS_READY else 503
        await self._respond(send, code, "health",
                            {"status": status, "release": self.release},
                            with_body=with_body)

    @staticmethod
    def _client_ip(scope) -> str:
        # Behind the Cloudflare tunnel the origin sees only tunnel traffic, so
        # CF-Connecting-IP is trustworthy; direct/local requests fall back to
        # the socket peer address. Never consulted for the health bypass —
        # reusing it there would contaminate the bypass boundary with a
        # spoofable header.
        for k, v in scope.get("headers", []):
            if k == b"cf-connecting-ip":
                return v.decode("latin-1")
        client = scope.get("client")
        return client[0] if client else "unknown"

    @staticmethod
    def _content_length(scope) -> int | None:
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    return int(v)
                except ValueError:
                    return None
        return None  # chunked/no length: reject rather than buffer unbounded

    # IPs live in memory only and are dropped after 10 minutes idle — this is
    # the retention promised in the README. It runs on a timer, not only on
    # the next request, so the promise also holds while the server is idle.
    IDLE_TTL_S = 600
    PRUNE_EVERY_S = 60

    def _prune(self, now: float) -> None:
        self._buckets = {k: v for k, v in self._buckets.items()
                         if now - v[1] < self.IDLE_TTL_S}

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self.PRUNE_EVERY_S)
            self._prune(time.monotonic())

    def _take_token(self, ip: str) -> bool:
        now = time.monotonic()
        if len(self._buckets) > 10_000:  # memory bound under a flood
            self._prune(now)
        bucket = self._buckets.setdefault(ip, [self.rate_capacity, now])
        tokens = min(self.rate_capacity,
                     bucket[0] + (now - bucket[1]) * self.rate_refill_per_s)
        bucket[1] = now
        if tokens < 1.0:
            bucket[0] = tokens
            return False
        bucket[0] = tokens - 1.0
        return True

    async def _reject(self, send, status: int, code: str, message: str,
                      retry_after: int | None = None) -> None:
        await self._respond(send, status, code,
                            {"error": message, "code": code}, retry_after)

    async def _respond(self, send, status: int, state: str, payload: dict,
                       retry_after: int | None = None, with_body: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = [(b"content-type", b"application/json; charset=utf-8"),
                   (b"content-length", str(len(body)).encode()),
                   # an edge or client cache replaying an old release id or an
                   # old kill 503 would silently break the cutover comparison
                   (b"cache-control", b"no-store"),
                   (b"x-krnd-release", self.release.encode("ascii", "replace")),
                   (b"x-krnd-state", state.encode())]
        if retry_after is not None:
            headers.append((b"retry-after", str(retry_after).encode()))
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        # HEAD keeps the headers of the equivalent GET and sends no body (RFC 9110)
        await send({"type": "http.response.body", "body": body if with_body else b""})
