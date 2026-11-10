#!/usr/bin/env python3
"""HTTP-mode regression test: transport, abuse guards, fail-closed embedding.

Launches the server subprocess with `--http` on a test port and verifies:
1. the v2 contract envelope over Streamable HTTP (MCP client end-to-end)
2. public log mode: no raw question text in new query-log records
3. body cap (413), rate limit (429), kill switch (503)
4. fail-closed: workersai backend without credentials returns an error dict,
   never BM25-only results (checked in-process)
5. S1 hotfix (UPDATE-PLAN-R1 §16 G-c): special-token questions succeed over
   HTTP, 4 of them concurrently; the server's own stdout/stderr and the query
   log never contain the question or its tokens (a sentinel token proves it);
   a lexical-leg failure returns a fixed error dict with no token in it

Run after any server/transport/guard change, alongside test_contract.py.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PORT = 8971
URL = f"http://127.0.0.1:{PORT}/mcp"
# Nonsense token that survives Kiwi as a foreign-word token; if it ever shows up
# in the server output or the query log, query text is leaking (decision #10).
SENTINEL = "zq7vkx"
SPECIAL_Q = f"{SENTINEL} 예산 137.6조원 1,000억원 p.183 한-미 협력"
LONG_SPECIAL_Q = ("국가연구개발사업 예산 137.6조원 1,000억원 p.183 5.0% 한-미 협력 e.g. 사례 " * 12)[:500]


def wait_ready(timeout: float = 120.0) -> None:
    """Wait for readiness, not just for a listening socket: since R1a the guard
    answers MCP requests 503 until the boot warm-up finishes, so returning as
    soon as the port accepts would make the first tool call flaky."""
    import httpx
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/readyz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("server did not become ready")


async def mcp_roundtrip() -> None:
    from fastmcp import Client
    async with Client(URL) as c:
        resp = (await c.call_tool(
            "search_rnd_facts", {"question": "학생인건비 잔액은 어떻게 반납하는가?"})).data
        assert resp["response_schema_version"] == "v2", resp
        assert resp["support_status"] == "not_assessed"
        assert resp["retrieval_status"] == "candidates_returned"
        assert all("scores" not in r for r in resp["results"])
        chunk = resp["results"][0]["chunk_id"]
        p = (await c.call_tool("get_passages", {"chunk_ids": [chunk]})).data
        assert p["passages"][0]["text"], "empty passage over HTTP"
    print("[http-mcp] v2 envelope + passage roundtrip over HTTP OK")


def _search_ok(resp: dict, label: str) -> None:
    # an error dict is a NORMAL tool result — check the data, not for exceptions
    assert "error" not in resp, f"{label}: {resp.get('error')}"
    assert resp["retrieval_status"] == "candidates_returned", label
    assert resp["results"], f"{label}: no results"


async def special_tokens_over_http() -> None:
    from fastmcp import Client
    async with Client(URL) as c:
        resp = (await c.call_tool("search_rnd_facts", {"question": SPECIAL_Q})).data
        _search_ok(resp, "special tokens")
        # concurrency: the HTTP guard allows 4 in flight — run 4 long special
        # queries at once (each expands the BM25 window several times)
        t0 = time.monotonic()
        results = await asyncio.gather(*[
            c.call_tool("search_rnd_facts", {"question": LONG_SPECIAL_Q, "max_results": 8})
            for _ in range(4)])
        for i, r in enumerate(results):
            _search_ok(r.data, f"concurrent #{i}")
    print(f"[s1-http] special tokens OK; 4 concurrent 500-char special queries in "
          f"{time.monotonic() - t0:.1f}s OK")


async def fail_closed_inprocess() -> None:
    # workersai without credentials must yield an error dict, not BM25 results
    os.environ["RND_FACTLINK_EMBED_BACKEND"] = "workersai"
    os.environ.pop("CF_ACCOUNT_ID", None)
    os.environ.pop("CF_WORKERS_AI_TOKEN", None)
    try:
        from fastmcp import Client

        from rnd_factlink_mcp.server import mcp
        async with Client(mcp) as c:
            resp = (await c.call_tool(
                "search_rnd_facts", {"question": "국가연구개발 투자방향"})).data
            assert "error" in resp and "fail-closed" in resp["error"], resp
            assert "results" not in resp, "degraded results leaked past fail-closed"
    finally:
        os.environ["RND_FACTLINK_EMBED_BACKEND"] = "local"
    print("[fail-closed] embed backend outage returns error, no degraded results OK")


async def lexical_fail_closed_inprocess() -> None:
    """A sqlite error inside the lexical leg -> fixed error dict, no dense-only
    results, and nothing that could quote the question in the response, the
    query log or the logging output. Run with raw query logging OFF (HTTP-mode
    equivalent): dev mode records questions by design."""
    import logging
    import sqlite3
    from io import StringIO

    from fastmcp import Client

    from rnd_factlink_mcp import search_hybrid, server
    sentinel = "SENTINELTOKEN_zq7v"
    log_path = ROOT / "vault" / "derived" / "reports" / "query_log.jsonl"
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    logging.getLogger().addHandler(handler)
    server._raw_query_logging = False
    real_con = search_hybrid._con

    def broken_con():
        raise sqlite3.OperationalError(f"no such column: {sentinel}")
    try:
        async with Client(server.mcp) as c:
            # warm the process caches through a normal call first, then break the leg
            _search_ok((await c.call_tool("search_rnd_facts", {"question": "국가연구개발 투자방향"})).data, "warm")
            search_hybrid._con = broken_con
            resp = (await c.call_tool("search_rnd_facts", {"question": f"{sentinel} 예산"})).data
        assert "error" in resp and "어휘 색인" in resp["error"] and resp.get("retryable") is True, resp
        assert "results" not in resp, "dense-only results leaked past fail-closed"
        assert sentinel not in json.dumps(resp, ensure_ascii=False), resp
        last = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        assert last["retrieval_status"] == "lexical_unavailable" and "question" not in last, last
        assert sentinel not in buf.getvalue(), "sentinel reached the logging output"
    finally:
        search_hybrid._con = real_con
        server._raw_query_logging = True
        logging.getLogger().removeHandler(handler)
    print("[fail-closed] lexical leg outage returns fixed error, no token in response/log OK")


def main() -> int:
    import httpx

    kill_path = Path(tempfile.mkdtemp()) / "kill"
    log_path = ROOT / "vault" / "derived" / "reports" / "query_log.jsonl"
    log_lines_before = log_path.read_text(encoding="utf-8").count("\n") \
        if log_path.exists() else 0

    env = {**os.environ,
           "PYTHONPATH": str(ROOT / "src"),
           "PORT": str(PORT),
           "RND_FACTLINK_HTTP_HOST": "127.0.0.1",
           "RND_FACTLINK_KILL_SWITCH": str(kill_path),
           "RND_FACTLINK_RATE_CAPACITY": "25",
           "RND_FACTLINK_RATE_REFILL": "0.1"}
    server_log = kill_path.parent / "server.log"
    proc = subprocess.Popen(
        [sys.executable, "-m", "rnd_factlink_mcp.server", "--http"],
        env=env, cwd=ROOT, stdout=open(server_log, "w"), stderr=subprocess.STDOUT)
    try:
        wait_ready()
        asyncio.run(mcp_roundtrip())
        asyncio.run(special_tokens_over_http())

        # 2. public log mode: the new records must not carry raw question text
        new = log_path.read_text(encoding="utf-8").splitlines()[log_lines_before:]
        assert new, "no query log written in HTTP mode"
        for line in new:
            rec = json.loads(line)
            assert "question" not in rec and "intent" not in rec, rec
            assert "question_len_bucket" in rec and "latency_ms" in rec, rec
            assert SENTINEL not in line
        print(f"[public-log] {len(new)} record(s), no raw query text OK")

        # 3a. body cap — Content-Length above the 64KB default
        r = httpx.post(URL, content=b"x" * 70_000,
                       headers={"content-type": "application/json"}, timeout=10.0)
        assert r.status_code == 413, r.status_code
        print("[guard] oversized body rejected 413 OK")

        # 3b. rate limit — burst past the 25-token bucket
        codes = []
        for _ in range(35):
            codes.append(httpx.post(URL, content=b"{}",
                                    headers={"content-type": "application/json"},
                                    timeout=10.0).status_code)
        assert 429 in codes, f"no 429 in burst: {sorted(set(codes))}"
        print(f"[guard] burst rate-limited (codes seen: {sorted(set(codes))}) OK")

        # 3c. kill switch — file present blocks everything with 503
        kill_path.write_text("paused")
        r = httpx.post(URL, content=b"{}",
                       headers={"content-type": "application/json"}, timeout=10.0)
        assert r.status_code == 503, r.status_code
        kill_path.unlink()
        r = httpx.post(URL, content=b"{}",
                       headers={"content-type": "application/json"}, timeout=10.0)
        assert r.status_code != 503, "kill switch did not release"
        print("[guard] kill switch blocks 503 and releases OK")
    finally:
        proc.terminate()
        proc.wait(timeout=30)  # uvicorn's graceful budget is 25s (GRACEFUL_SHUTDOWN_S)

    # 5. the server's own output must not carry the question or its tokens
    out = server_log.read_text(encoding="utf-8", errors="replace")
    assert out, "server produced no output at all (log capture broken?)"
    for needle in (SENTINEL, "137.6", "1,000", "한-미"):
        assert needle not in out, f"query token {needle!r} found in server stdout/stderr"
    assert "Traceback" not in out, "server logged a traceback"
    print(f"[privacy] server stdout/stderr ({len(out)} chars): no query tokens, no traceback OK")

    asyncio.run(fail_closed_inprocess())
    asyncio.run(lexical_fail_closed_inprocess())
    print("HTTP REGRESSION TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
