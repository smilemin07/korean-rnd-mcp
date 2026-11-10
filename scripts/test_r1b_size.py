#!/usr/bin/env python3
"""R1b size gate (UPDATE-PLAN-R1 §12 D-A) — the contract budget is PROVED, not enforced
by truncation at runtime (runtime truncation would break the result-invariance gate).

Three separate measurements, never one number:
  1. tool dict serialized (json.dumps, ensure_ascii=False): bytes and BGE-token estimate
     — the contract budget surface (DEFAULT 4,000 / HARD CAP 8,000 tokens)
  2. connector instructions: chars and BGE tokens vs the sealed regression bound
  3. corpus property behind the bound: no chunk's excerpt exceeds EXCERPT_MAX_TOKENS
     and no chunk's FIRST LINE does (the _excerpt loop admits an over-long first line)
Wire bytes (FastMCP content + structuredContent) are measured in test_public_bundle.py
over real HTTP and recorded, not asserted.

BGE tokens are a proxy — the client model's tokenizer differs. Nothing here is exposed
in a response.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

QUERIES = [
    "학생인건비 잔액은 어떻게 반납하는가?", "정부 연구개발 투자방향 및 기준의 중점 투자 분야",
    "제7차 건설기술진흥 기본계획의 추진 전략", "국가연구개발사업 예산 투자 방향",
    "출연연 연구과제중심운영제도 개편 내용", "재난안전 연구개발 참여 부처 현황",
    "국가전략기술 특별법에 따른 지정 절차", "물류시설개발 종합계획의 계획 기간",
    "제4차 대중교통기본계획의 비전", "건설 분야 기술수준",
    "2026년도 국가연구개발사업 예산 배분 조정안 주요 내용", "기후변화 적응 기술개발 전략",
]


async def main() -> int:
    from fastmcp import Client
    import rnd_factlink_mcp.server as srv
    from rnd_factlink_mcp.paths import VAULT
    from rnd_factlink_mcp.tokenize_ko import bge_token_count

    worst = (0, 0, None)
    async with Client(srv.mcp) as c:
        for q in QUERIES:
            for f in ("all", "current"):
                resp = (await c.call_tool("search_rnd_facts",
                                          {"question": q, "max_results": 8,
                                           "plan_status_filter": f})).data
                blob = json.dumps(resp, ensure_ascii=False)
                toks = bge_token_count(blob)
                if toks > worst[0]:
                    worst = (toks, len(blob.encode("utf-8")), (q, f, len(resp["results"])))
        # passages: 8 full chunks — the other budgeted surface
        resp = (await c.call_tool("search_rnd_facts", {"question": QUERIES[9], "max_results": 8})).data
        ids = [r["chunk_id"] for r in resp["results"]]
        p = (await c.call_tool("get_passages", {"chunk_ids": ids})).data
        pblob = json.dumps(p, ensure_ascii=False)
        ptoks = bge_token_count(pblob)
    print(f"[size-1] search worst case: {worst[0]} BGE tokens / {worst[1]} bytes "
          f"(q={worst[2][0][:24]!r} f={worst[2][1]} n={worst[2][2]})")
    assert worst[0] <= srv.HARD_CAP_TOKENS, "search response exceeds the hard cap"
    print(f"[size-1] passages x{len(ids)}: {ptoks} BGE tokens / {len(pblob.encode())} bytes "
          f"(passage text budget {srv.HARD_CAP_TOKENS} applies to text only)")

    ins = srv.mcp.instructions
    itoks = bge_token_count(ins)
    print(f"[size-2] instructions: {len(ins)} chars / {itoks} BGE tokens (bound {srv.INSTRUCTIONS_MAX_CHARS} chars / 600 tokens)")
    assert len(ins) <= srv.INSTRUCTIONS_MAX_CHARS and itoks <= 600

    con = sqlite3.connect(VAULT / "index" / "lexical.db")
    n = over = first_over = 0
    mx = 0
    for (text,) in con.execute("SELECT text_raw FROM chunks WHERE evidence_eligible = 1"):
        n += 1
        _, t, _ = srv._excerpt(text)
        mx = max(mx, t)
        if t > srv.EXCERPT_MAX_TOKENS:
            over += 1
        lines = text.splitlines()
        if lines and bge_token_count(lines[0]) > srv.EXCERPT_MAX_TOKENS:
            first_over += 1
    con.close()
    print(f"[size-3] corpus: {n} citable chunks, max excerpt {mx} tokens, over-cap {over}, "
          f"first-line-over-cap {first_over}")
    assert over == 0 and first_over == 0, "excerpt bound violated — the 8,000 proof no longer holds"
    print("R1B SIZE GATE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
