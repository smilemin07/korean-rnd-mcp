#!/usr/bin/env python3
"""Live smoke against the public endpoint (post-deploy verification).

Usage: .venv/bin/python scripts/smoke_public_url.py [base_url] [--expect-release <12hex>] [--degraded]

Canaries are checked on the response DATA, not on transport errors: the server
returns error dicts as normal results, so "no exception" proves nothing.
--degraded: the running release is a known pre-S1-fix build (rollback state,
UPDATE-PLAN-R1 §16) — the special-token canaries are then reported as expected
failures instead of asserted. Only use it after confirming that release id.
"""

import asyncio
import sys

args = [a for a in sys.argv[1:]]
DEGRADED = "--degraded" in args
EXPECT_RELEASE = args[args.index("--expect-release") + 1] if "--expect-release" in args else None
positional = [a for i, a in enumerate(args) if not a.startswith("--")
              and not (i > 0 and args[i - 1] == "--expect-release")]
BASE = positional[0] if positional else "https://korean-rnd-mcp.rndmanagers.org"

# (label, question, chunk id that must be among the results or None)
CANARIES = [
    ("basic", "학생인건비 잔액은 어떻게 반납하는가?", None),
    # S1 hotfix (§16): a decimal used to fail the whole tool call. The anchor is
    # the 투자전략 table chunk (same chunk id in dev and public indexes).
    ("decimal", "137.6조원 규모", "d_7d310470a623_c0044"),
    ("thousands", "1,000억원 이상 사업", None),
]
SPECIAL = {"decimal", "thousands"}


def check_search(label: str, resp: dict, anchor: str | None) -> None:
    assert "error" not in resp, f"{label}: tool returned an error dict: {resp.get('error')}"
    assert resp["response_schema_version"] == "v2", label
    assert resp["retrieval_status"] == "candidates_returned", (label, resp["retrieval_status"])
    assert resp["results"], f"{label}: no results"
    for r in resp["results"]:
        assert "scores" not in r, "raw scores leaked"
        assert r["citation"]["citation_allowed"] is True
    if anchor:
        ids = [r["chunk_id"] for r in resp["results"]]
        assert anchor in ids, f"{label}: anchor {anchor} not in {ids}"


async def main() -> int:
    import httpx
    from fastmcp import Client

    # release identity first: GET /mcp answers 405 with the release header
    r = httpx.get(f"{BASE}/mcp", timeout=15.0)
    release = r.headers.get("x-krnd-release", "-")
    assert r.status_code == 405, f"GET /mcp expected 405, got {r.status_code}"
    if EXPECT_RELEASE:
        assert release == EXPECT_RELEASE, f"release header {release} != expected {EXPECT_RELEASE}"
    print(f"[edge] GET /mcp 405, X-KRND-Release {release}"
          + (" (matches expected)" if EXPECT_RELEASE else ""))

    async with Client(f"{BASE}/mcp") as c:
        tools = await c.list_tools()
        names = {t.name for t in tools}
        assert {"search_rnd_facts", "get_passages", "get_document_metadata"} <= names, names
        basic = None
        for label, question, anchor in CANARIES:
            resp = (await c.call_tool("search_rnd_facts", {"question": question})).data
            try:
                check_search(label, resp, anchor)
            except AssertionError as e:
                if DEGRADED and label in SPECIAL:
                    print(f"[mcp] canary {label}: expected failure in degraded (pre-fix) state — {e}")
                    continue
                raise
            print(f"[mcp] canary {label}: {len(resp['results'])} results OK")
            if label == "basic":
                basic = resp
        assert basic is not None
        chunk = basic["results"][0]
        p = (await c.call_tool("get_passages", {"chunk_ids": [chunk["chunk_id"]]})).data
        assert p["passages"][0].get("text_complete") is True
        with_url = [r for r in basic["results"] if r["citation"].get("source_url")]
        # the canary's top documents are URL-mapped; zero means the served
        # registry did not load (silent path regression, 2026-08-24)
        assert with_url, "no result carried source_url — URL registry not loaded"
        print(f"[mcp] passage roundtrip OK, {len(with_url)} basic results with source_url")

    # guard sanity: oversized body must be rejected before the app
    r = httpx.post(f"{BASE}/mcp", content=b"x" * 70_000,
                   headers={"content-type": "application/json"}, timeout=15.0)
    assert r.status_code == 413, r.status_code
    print("[guard] 413 on oversized body OK")

    # burst a few tiny posts: the limiter must answer 429 at least once
    codes = [httpx.post(f"{BASE}/mcp", content=b"{}",
                        headers={"content-type": "application/json"},
                        timeout=15.0).status_code for _ in range(15)]
    assert 429 in codes, f"no 429 in burst: {sorted(set(codes))}"
    print(f"[guard] burst codes: {sorted(set(codes))} (429 seen) OK")
    print("PUBLIC URL SMOKE PASSED" + (" (degraded mode)" if DEGRADED else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
