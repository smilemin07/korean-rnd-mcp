#!/usr/bin/env python3
"""R1a regression test (UPDATE-PLAN-R1 §1 C7a): warm-up, readiness, gate.

Covers:
 1. state machine — warming → ready, exception → failed (with the failing
    stage), deadline → degraded and then promotion back to ready
 2. warm pass — never embeds remotely, so no neuron budget charge and no
    query-log record
 3. gate at the ASGI layer — /mcp blocked while warming, open once serving,
    non-MCP paths untouched
 4. health bypass — /livez and /readyz answer through kill switch and an
    exhausted rate bucket, identically for every source (a forged
    CF-Connecting-IP must not change the outcome)
 5. /readyz status contract — 200 only for `ready`; degraded/failed are 503
 6. release identity — format, and X-KRND-Release/X-KRND-State/Cache-Control on
    guard responses and on downstream responses
 7. end-to-end over real HTTP, including the kill switch

Run after any change to readiness.py, http_guard.py or the server's HTTP mode.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_STATE_DIR = tempfile.mkdtemp()
os.environ["RND_FACTLINK_READINESS_FILE"] = str(Path(_STATE_DIR) / "readiness.json")

from rnd_factlink_mcp import readiness  # noqa: E402
from rnd_factlink_mcp.http_guard import AbuseGuard  # noqa: E402

PORT = 8973
BASE = f"http://127.0.0.1:{PORT}"


# --------------------------------------------------------------------------- #
# ASGI harness
# --------------------------------------------------------------------------- #

async def call(guard, method="POST", path="/mcp", headers=None, client=("10.0.0.1", 5000)):
    """Drive the middleware directly and collect the response."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path,
             "headers": headers or [(b"content-length", b"2")], "client": client}
    await guard(scope, receive, send)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    hdrs = {k.decode(): v.decode() for k, v in (start or {}).get("headers", [])}
    return (start or {}).get("status"), hdrs, body


def make_guard(status_value, kill_path="", downstream=None):
    """Guard whose readiness state and downstream app are both controlled."""
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 404,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"Not Found"})

    guard = AbuseGuard(downstream or app, kill_switch_path=kill_path, max_body=65536,
                       rate_capacity=10, rate_refill_per_s=0.5, max_concurrency=4,
                       status_fn=lambda: status_value[0], release="abc123def456")
    return guard, calls


# --------------------------------------------------------------------------- #
# 1-2. state machine and warm-pass side effects
# --------------------------------------------------------------------------- #

def test_state_machine() -> None:
    assert readiness.status() == readiness.STATUS_STARTING

    async def happy() -> None:
        stages_seen = []

        def warm(stage):
            stage("one")
            stages_seen.append("one")
            stage("two")
            stages_seen.append("two")

        readiness.start_warmup(warm)
        assert readiness.status() == readiness.STATUS_WARMING
        assert not readiness.serving_allowed(), "gate must be closed while warming"
        for _ in range(200):
            if readiness.status() == readiness.STATUS_READY:
                break
            await asyncio.sleep(0.02)
        assert readiness.status() == readiness.STATUS_READY, readiness.state()
        assert readiness.is_ready() and readiness.serving_allowed()
        assert stages_seen == ["one", "two"]
        assert set(readiness.state()["stages_ms"]) >= {"one", "two"}

    asyncio.run(happy())
    print("[state] warming → ready, gate closed while warming OK")

    # failure keeps the failing stage and opens the gate (bounded fail-open)
    readiness._set(status=readiness.STATUS_STARTING, stage=None, error=None,
                   stages_ms={}, note=None)

    async def failing() -> None:
        def warm(stage):
            stage("metadata")
            raise RuntimeError("boom")

        readiness.start_warmup(warm)
        for _ in range(200):
            if readiness.status() == readiness.STATUS_FAILED:
                break
            await asyncio.sleep(0.02)
        state = readiness.state()
        assert state["status"] == readiness.STATUS_FAILED, state
        assert state["stage"] == "metadata", state
        assert "RuntimeError: boom" in state["error"], state
        assert readiness.serving_allowed(), "failed warm-up must fail OPEN"
        assert not readiness.is_ready(), "failed must never report ready"

    asyncio.run(failing())
    print("[state] exception → failed(stage recorded), gate opens, not ready OK")

    # deadline: gate opens as degraded, then the slow warm promotes it to ready
    readiness._set(status=readiness.STATUS_STARTING, stage=None, error=None,
                   stages_ms={}, note=None)
    os.environ["RND_FACTLINK_WARM_DEADLINE_S"] = "0.2"

    async def slow() -> None:
        def warm(stage):
            stage("slow")
            time.sleep(0.8)

        readiness.start_warmup(warm)
        await asyncio.sleep(0.45)
        assert readiness.status() == readiness.STATUS_DEGRADED, readiness.state()
        assert readiness.serving_allowed(), "degraded must serve"
        assert not readiness.is_ready()
        for _ in range(200):
            if readiness.status() == readiness.STATUS_READY:
                break
            await asyncio.sleep(0.02)
        assert readiness.status() == readiness.STATUS_READY, readiness.state()
        assert readiness.state()["note"], "late completion should be noted"

    asyncio.run(slow())
    os.environ.pop("RND_FACTLINK_WARM_DEADLINE_S")
    print("[state] deadline → degraded (serving), late finish → ready OK")

    # diagnostics land in the container-local file, not in any response
    disk = json.loads(Path(os.environ["RND_FACTLINK_READINESS_FILE"]).read_text(encoding="utf-8"))
    assert {"status", "stage", "stages_ms", "release"} <= set(disk), disk
    print("[state] diagnostics written to the readiness file OK")

    # the deadline task and the warm thread write the diagnostics concurrently;
    # a shared stages_ms dict used to make that a RuntimeError (json.dumps with an
    # indent iterates in Python), which would have flipped a healthy warm to failed
    readiness._set(status=readiness.STATUS_STARTING, stage=None, error=None,
                   stages_ms={}, note=None)
    os.environ["RND_FACTLINK_WARM_DEADLINE_S"] = "0.1"

    async def churning() -> None:
        def warm(stage):
            for i in range(400):
                stage(f"s{i}")
                time.sleep(0.001)

        readiness.start_warmup(warm)
        for _ in range(400):
            if readiness.status() == readiness.STATUS_READY:
                break
            await asyncio.sleep(0.02)
        assert readiness.status() == readiness.STATUS_READY, readiness.state()

    asyncio.run(churning())
    os.environ.pop("RND_FACTLINK_WARM_DEADLINE_S")
    disk = json.loads(Path(os.environ["RND_FACTLINK_READINESS_FILE"]).read_text(encoding="utf-8"))
    assert len(disk["stages_ms"]) >= 100, len(disk["stages_ms"])
    print("[state] concurrent stage writes stay valid JSON, warm still succeeds OK")


def test_state_file_writes_are_thread_safe() -> None:
    """Two writers at once — the warm thread recording a stage and the deadline
    task flipping the status — must never raise out of the diagnostics writer or
    leave the file unparseable.

    Scope note: this asserts the observable contract, not the internal cause. It
    does not distinguish "snapshot copies stages_ms" from "the writer swallowed a
    RuntimeError and skipped that write", because _write_state_file catches
    everything by design. The copy is what keeps those writes from being lost.
    """
    readiness._set(status=readiness.STATUS_WARMING, stage="x", stages_ms={},
                   error=None, note=None)
    errors: list = []
    stop = threading.Event()

    def writer():
        try:
            while not stop.is_set():
                readiness._write_state_file()
        except Exception as e:  # noqa: BLE001 — the whole point is to catch it
            errors.append(e)

    def mutator():
        # time-bounded, not count-bounded: a short burst finishes before the
        # writer has serialized anything and the race never gets a chance
        try:
            i, t0 = 0, time.monotonic()
            while time.monotonic() - t0 < 1.2:
                with readiness._lock:
                    readiness._state["stages_ms"][f"s{i}"] = i
                i += 1
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    w, m = threading.Thread(target=writer), threading.Thread(target=mutator)
    w.start()
    m.start()
    m.join()
    stop.set()
    w.join()
    assert not errors, errors
    json.loads(Path(os.environ["RND_FACTLINK_READINESS_FILE"]).read_text(encoding="utf-8"))
    print("[state] concurrent diagnostics writers never raise, file stays valid OK")


def test_deadline_cannot_demote_a_successful_warm() -> None:
    """The deadline task decides to degrade, then loses the race to a worker
    that finishes first. Without a guarded transition it would overwrite `ready`
    and the server would answer /readyz 503 forever despite a healthy warm."""
    readiness._set(status=readiness.STATUS_READY, stage=None, error=None,
                   stages_ms={}, note=None)
    applied = readiness._set(only_if_status=(readiness.STATUS_WARMING,),
                             status=readiness.STATUS_DEGRADED, error="late deadline")
    assert applied is False, "a stale deadline was allowed to demote ready"
    assert readiness.status() == readiness.STATUS_READY, readiness.state()
    print("[state] a late deadline cannot demote a warm that already succeeded OK")


def test_warm_pass_has_no_side_effects() -> None:
    """The warm pass must not spend neurons or write a query-log record."""
    from rnd_factlink_mcp import embed_query, server
    log = server.QUERY_LOG
    before_log = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
    budget = embed_query.BUDGET_PATH
    before_budget = budget.read_text(encoding="utf-8") if budget.exists() else None

    calls = []
    original = embed_query.embed_query
    embed_query.embed_query = lambda text: calls.append(text)  # must never run
    try:
        server._warm_pass(lambda name: None)
    finally:
        embed_query.embed_query = original

    assert not calls, "warm pass embedded a query remotely"
    after_log = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
    assert after_log == before_log, "warm pass wrote a query-log record"
    after_budget = budget.read_text(encoding="utf-8") if budget.exists() else None
    assert after_budget == before_budget, "warm pass charged the neuron budget"
    print("[warm] no remote embed, no query-log record, no budget charge OK")


# --------------------------------------------------------------------------- #
# 3-6. gate, health bypass, headers
# --------------------------------------------------------------------------- #

def test_gate_and_health() -> None:
    state = [readiness.STATUS_WARMING]
    guard, downstream_calls = make_guard(state)

    status, headers, body = asyncio.run(call(guard, "POST", "/mcp"))
    assert status == 503 and json.loads(body)["code"] == "not_ready", (status, body)
    assert headers["retry-after"] == "5" and headers["x-krnd-state"] == "not_ready"
    assert not downstream_calls, "request reached the app while not ready"

    # a non-MCP path keeps its normal routing during warm-up
    status, _h, _b = asyncio.run(call(guard, "GET", "/something"))
    assert status == 404 and downstream_calls == ["/something"], (status, downstream_calls)
    print("[gate] /mcp 503 not_ready while warming, other paths untouched OK")

    for serving in (readiness.STATUS_READY, readiness.STATUS_DEGRADED, readiness.STATUS_FAILED):
        state[0] = serving
        status, headers, _b = asyncio.run(call(guard, "POST", "/mcp"))
        assert status == 404, (serving, status)  # reached the stub app
        assert headers["x-krnd-release"] == "abc123def456", headers
    print("[gate] ready/degraded/failed all serve, downstream carries release OK")

    # /readyz status contract
    for value, expected in ((readiness.STATUS_READY, 200), (readiness.STATUS_WARMING, 503),
                            (readiness.STATUS_DEGRADED, 503), (readiness.STATUS_FAILED, 503)):
        state[0] = value
        status, headers, body = asyncio.run(call(guard, "GET", "/readyz"))
        payload = json.loads(body)
        assert status == expected, (value, status)
        assert payload == {"status": value, "release": "abc123def456"}, payload
        assert headers["cache-control"] == "no-store"
    print("[health] /readyz 200 only for ready; body carries no diagnostics OK")

    # trailing slash must not fall through to the router
    status, _h, _b = asyncio.run(call(guard, "GET", "/livez/"))
    assert status == 200, status

    # HEAD probes get the GET headers and no body (RFC 9110)
    state[0] = readiness.STATUS_READY
    status, headers, body = asyncio.run(call(guard, "HEAD", "/readyz"))
    assert status == 200 and body == b"", (status, body)
    assert headers["x-krnd-state"] == "health" and int(headers["content-length"]) > 0
    print("[health] trailing slash and HEAD handled OK")


def test_health_bypasses_kill_and_rate() -> None:
    kill = Path(tempfile.mkdtemp()) / "kill"
    kill.write_text("paused")
    state = [readiness.STATUS_READY]
    guard, _calls = make_guard(state, kill_path=str(kill))

    status, headers, body = asyncio.run(call(guard, "POST", "/mcp"))
    assert status == 503 and json.loads(body)["code"] == "killed", body
    assert headers["x-krnd-state"] == "killed"
    assert headers["x-krnd-release"] == "abc123def456", "kill 503 must carry the release"

    # every source must see the same health answer — internal (no CF header),
    # published-port style, and a forged CF-Connecting-IP
    sources = [
        ("internal", [], ("127.0.0.1", 1)),
        ("gateway", [], ("172.18.0.1", 2)),
        ("forged-header", [(b"cf-connecting-ip", b"127.0.0.1")], ("172.18.0.1", 3)),
    ]
    for name, headers_in, client in sources:
        for path in ("/livez", "/readyz"):
            status, _h, _b = asyncio.run(
                call(guard, "GET", path, headers=headers_in, client=client))
            assert status == 200, (name, path, status)
    print("[health] answers through the kill switch, identically for every source OK")

    # drain the bucket on a guard with no maintenance pause (the kill switch is
    # checked first, so it would mask the rate limit), then confirm health answers
    open_guard, _ = make_guard([readiness.STATUS_READY])
    for _ in range(15):
        asyncio.run(call(open_guard, "GET", "/other", client=("9.9.9.9", 1)))
    status, _h, _b = asyncio.run(call(open_guard, "GET", "/other", client=("9.9.9.9", 1)))
    assert status == 429, status
    status, _h, _b = asyncio.run(call(open_guard, "GET", "/readyz", client=("9.9.9.9", 1)))
    assert status == 200, status
    print("[health] answers through an exhausted rate bucket OK")


def test_release_identity() -> None:
    rid = readiness.release_id()
    assert rid == "dev" or (len(rid) == 12 and all(c in "0123456789abcdef" for c in rid)), rid
    a = readiness.compute_release_id({"files": {"src/x.py": "aa", "src/y.py": "bb"}})
    b = readiness.compute_release_id({"files": {"src/y.py": "bb", "src/x.py": "aa"}})
    c = readiness.compute_release_id({"files": {"src/x.py": "aa", "src/y.py": "cc"}})
    assert a == b, "id must not depend on key order"
    assert a != c, "a changed file must change the id"
    assert readiness.compute_release_id({}) == ""
    print(f"[release] id={rid}, order-independent, content-sensitive OK")


# --------------------------------------------------------------------------- #
# 7. end-to-end
# --------------------------------------------------------------------------- #

def test_http_end_to_end() -> None:
    import httpx

    kill = Path(tempfile.mkdtemp()) / "kill"
    env = {**os.environ,
           "PYTHONPATH": str(ROOT / "src"),
           "PORT": str(PORT),
           "RND_FACTLINK_HTTP_HOST": "127.0.0.1",
           "RND_FACTLINK_KILL_SWITCH": str(kill),
           "RND_FACTLINK_READINESS_FILE": str(Path(_STATE_DIR) / "e2e.json")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "rnd_factlink_mcp.server", "--http"],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        ready = False
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{BASE}/readyz", timeout=2.0)
                if r.status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        assert ready, "server never became ready"
        payload = r.json()
        assert payload["status"] == "ready" and payload["release"], payload
        assert r.headers["x-krnd-state"] == "health"
        assert r.headers["cache-control"] == "no-store"
        release = r.headers["x-krnd-release"]
        print(f"[e2e] /readyz 200 ready, release={release} OK")

        live = httpx.get(f"{BASE}/livez", timeout=5.0)
        assert live.status_code == 200 and live.json() == {"status": "live"}, live.text

        # over real HTTP, not just the ASGI layer: a body sent for HEAD would be
        # an h11 protocol error, so this is the check that the framing is right
        head = httpx.head(f"{BASE}/readyz", timeout=5.0)
        assert head.status_code == 200 and head.content == b"", head.status_code
        assert head.headers["x-krnd-release"] == release, dict(head.headers)
        print("[e2e] HEAD /readyz framed correctly over real HTTP OK")

        served = httpx.post(f"{BASE}/mcp", content=b"{}",
                            headers={"content-type": "application/json"}, timeout=10.0)
        assert served.status_code != 503, served.status_code
        assert served.headers.get("x-krnd-release") == release, dict(served.headers)
        print("[e2e] MCP path served, downstream response carries the release OK")

        kill.write_text("paused")
        killed = httpx.post(f"{BASE}/mcp", content=b"{}",
                            headers={"content-type": "application/json"}, timeout=10.0)
        assert killed.status_code == 503 and killed.json()["code"] == "killed", killed.text
        assert killed.headers["x-krnd-release"] == release
        still = httpx.get(f"{BASE}/readyz", timeout=5.0)
        assert still.status_code == 200, "health must answer during maintenance"
        kill.unlink()
        print("[e2e] kill switch: MCP 503 killed, health still 200 OK")

        # the container health probe is the Dockerfile's HEALTHCHECK command
        probe = subprocess.run(
            [sys.executable, "-m", "rnd_factlink_mcp.healthcheck"],
            env={**env, "PORT": str(PORT)}, cwd=ROOT)
        assert probe.returncode == 0, probe.returncode
        kill.write_text("paused")
        probe_killed = subprocess.run(
            [sys.executable, "-m", "rnd_factlink_mcp.healthcheck"],
            env={**env, "PORT": str(PORT)}, cwd=ROOT)
        assert probe_killed.returncode == 0, "health must stay green during maintenance"
        kill.unlink()
        print("[e2e] healthcheck module exits 0 when ready, incl. during a kill OK")
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def main() -> int:
    test_state_machine()
    test_state_file_writes_are_thread_safe()
    test_deadline_cannot_demote_a_successful_warm()
    test_warm_pass_has_no_side_effects()
    test_gate_and_health()
    test_health_bypasses_kill_and_rate()
    test_release_identity()
    test_http_end_to_end()
    print("READINESS (R1a / C7a) TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
