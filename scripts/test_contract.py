#!/usr/bin/env python3
"""Contract regression test for the v2 MCP response contract (docs/SCHEMA.md §7).

Covers (per the reviewed plan):
- hard-negative real cases (adjacent-statistic): no support judgment leaks,
  only citable results, no raw scores in responses
- retrieval canary: a known direct-evidence question must surface its document
  within max_results, and the passage round-trip must be complete (prevents an
  "always refuse" useless system from passing)
- eligibility before cutoff: summary-flavored query still returns only citable
  results while the summary's family is discoverable via metadata
- gap independence: extraction gaps never change support_status
- input validation and no_candidates wording

Deliberately NOT asserted: ranks, cosine values, chunk counts (fragile).

R1b additions (UPDATE-PLAN-R1 §3/§12): contract fields present and typed,
answer_requirements text derived by the one function, instructions bound,
manifest referential integrity, closed label mappings, page-label cases,
strict-client matrix with its pre-declared cell expectations.
"""

import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# real hard-negative cases from the 2026-08-09 live session (query log verbatim)
HARD_NEGATIVES = [
    {"question": "정부 연구개발사업에 참여하는 연구원 수는 몇 명인가?",
     "intent": "우리나라 정부 R&D 사업 참여 연구원(연구책임자 포함) 총 인원 규모"},
    {"question": "국가연구개발사업 참여연구자 수는 몇 명인가?",
     "intent": "국가연구개발사업(정부 R&D 과제)에 실제 참여한 연구자 인원 통계(조사·분석 기준)"},
]

# retrieval canary: known direct evidence in corpus (제7차 건설기술진흥 기본계획 p.10).
# The question shares only generic tokens (건설, 기술) with the title; the body
# chunk (p.10) ranks first even with title/inst BM25 weights at 0.
CANARY = {"question": "건설산업 생산성을 높이기 위한 첨단기술 확산 목표는?",
          "expected_title_part": "제7차 건설기술진흥 기본계획",
          "expected_phrase": "첨단기술 확산을 통해 다시 도약하는 건설산업"}


def check_v2_envelope(resp: dict) -> None:
    assert resp.get("response_schema_version") == "v2", resp.keys()
    assert "evidence_status" not in resp, "v1 evidence_status leaked"
    assert resp["support_status"] == "not_assessed"
    assert resp["direct_answer_check_required"] is True
    assert resp["corpus_absence_claim_allowed"] is False
    assert resp["retrieval_status"] in ("candidates_returned", "no_candidates")
    for r in resp["results"]:
        assert "scores" not in r and "dense_cosine" not in str(r.keys()), "raw scores leaked"
        assert "excerpt_truncated" in r
        c = r["citation"]
        assert c["citation_allowed"] is True, f"non-citable result leaked: {c['title']}"
        assert c["authority_tier"] in (1, 2), f"tier-3 result leaked: {c['title']}"


async def main() -> int:
    from fastmcp import Client
    from rnd_factlink_mcp.server import mcp

    async with Client(mcp) as c:
        # 1. hard negatives: safety contract
        for case in HARD_NEGATIVES:
            resp = (await c.call_tool("search_rnd_facts", case)).data
            check_v2_envelope(resp)
            print(f"[hard-negative] {case['question'][:30]}… "
                  f"status={resp['retrieval_status']} n={len(resp['results'])} OK")

        # 2. retrieval canary: usefulness preserved (anti-useless-system)
        resp = (await c.call_tool("search_rnd_facts", {"question": CANARY["question"]})).data
        check_v2_envelope(resp)
        titles = [r["citation"]["title"] for r in resp["results"]]
        assert any(CANARY["expected_title_part"] in t for t in titles), \
            f"canary doc missing from top results: {titles}"
        canary_chunk = next(r for r in resp["results"]
                            if CANARY["expected_title_part"] in r["citation"]["title"])
        p = (await c.call_tool("get_passages", {"chunk_ids": [canary_chunk["chunk_id"]]})).data
        passage = p["passages"][0]
        assert passage.get("text_complete") is True, "canary passage truncated"
        assert CANARY["expected_phrase"] in passage["text"]
        print(f"[canary] doc found in top results, roundtrip complete "
              f"({len(passage['text'])} chars, sha={hashlib.sha256(passage['text'].encode()).hexdigest()[:8]}) OK")

        # 3. eligibility: summary-flavored query returns only citable results,
        #    while the summary is still discoverable via family metadata
        resp = (await c.call_tool("search_rnd_facts",
                                  {"question": "2024년도 연구개발활동조사 결과 요약 기술격차"})).data
        check_v2_envelope(resp)
        meta = (await c.call_tool("get_document_metadata",
                                  {"doc_id": resp["results"][0]["citation"]["doc_id"]})).data
        fam_tiers = [m["authority_tier"] for m in meta["family_members"]]
        print(f"[eligibility] all results citable, family tiers={sorted(fam_tiers)} "
              f"(tier 3 visible in metadata: {3 in fam_tiers}) OK")

        # 4. gap independence: gaps do not change support_status
        resp = (await c.call_tool("search_rnd_facts",
                                  {"question": "글로벌 R&D 추진 방향 발표자료의 핵심 내용은?"})).data
        check_v2_envelope(resp)
        assert resp["support_status"] == "not_assessed"
        for g in resp["detected_extraction_gaps"]:
            assert set(g) >= {"doc_id", "title", "gap_type", "reason"}, g
        print(f"[gap] {len(resp['detected_extraction_gaps'])} structured gap(s), "
              f"support_status unchanged OK")

        # 5. input validation
        bad = (await c.call_tool("search_rnd_facts", {"question": "  "})).data
        assert "error" in bad
        print("[validation] empty question rejected OK")

        # 6. source_url plumbing: absent when unmapped, present when mapped,
        #    never fabricated (in-memory mapping only — no fake URL on disk)
        import rnd_factlink_mcp.server as srv
        # the registry must actually load from disk — a wrong SOURCE_URLS_PATH
        # fails silently, dropping every citation URL (regression 2026-08-24)
        assert srv.SOURCE_URLS_PATH.is_file(), f"missing {srv.SOURCE_URLS_PATH}"
        assert len(srv._source_urls()) > 100, "URL registry loaded empty/short"
        srv._source_urls_cache = None
        canary_doc = canary_chunk["citation"]["doc_id"]
        srv._source_urls_cache = {}  # in-memory: nothing mapped
        try:
            resp = (await c.call_tool("search_rnd_facts",
                                      {"question": CANARY["question"]})).data
            assert all("source_url" not in r["citation"] for r in resp["results"]), \
                "source_url present without a mapping"
            assert resp["candidate_source_manifest"] and all(
                m["source_url"] is None and m["source_url_status"] == "unavailable"
                for m in resp["candidate_source_manifest"]), resp["candidate_source_manifest"]
        finally:
            srv._source_urls_cache = None
        srv._source_urls_cache = {canary_doc: "https://example.invalid/원문"}
        try:
            resp = (await c.call_tool("search_rnd_facts",
                                      {"question": CANARY["question"]})).data
            mapped = next(r for r in resp["results"]
                          if r["citation"]["doc_id"] == canary_doc)
            assert mapped["citation"].get("source_url") == "https://example.invalid/원문"
            meta = (await c.call_tool("get_document_metadata", {"doc_id": canary_doc})).data
            assert meta.get("source_url") == "https://example.invalid/원문"
            others = [r for r in resp["results"] if r["citation"]["doc_id"] != canary_doc]
            assert all("source_url" not in r["citation"] for r in others), \
                "source_url leaked to unmapped docs"
        finally:
            srv._source_urls_cache = None
        assert "source_url" in srv.mcp.instructions, "instructions missing source_url rule"
        print("[source_url] mapped/unmapped behavior + instructions rule OK")

        # 7. get_passages runtime allowlist: a non-citable (tier-3) chunk must be
        #    refused even though it exists in the dev index (defense in depth
        #    behind the physically filtered public bundle)
        import sqlite3
        from rnd_factlink_mcp.paths import VAULT
        con = sqlite3.connect(VAULT / "index" / "lexical.db")
        t3 = con.execute(
            "SELECT chunk_id FROM chunks WHERE evidence_eligible = 0 LIMIT 1").fetchone()
        con.close()
        if t3 is None:
            print("[passage-allowlist] no ineligible chunk in index — skipped")
        else:
            p = (await c.call_tool("get_passages", {"chunk_ids": [t3[0]]})).data
            entry = p["passages"][0]
            assert "text" not in entry and "error" in entry, entry
            assert "제공 대상" in entry["error"], entry
            print("[passage-allowlist] ineligible chunk refused OK")

        await r1b_checks(c)

    print("CONTRACT REGRESSION TEST PASSED")
    return 0


# --------------------------------------------------------------------------- #
# R1b
# --------------------------------------------------------------------------- #
LEGACY_TOP_LEVEL = {"response_schema_version", "retrieval_status", "support_status",
                    "direct_answer_check_required", "corpus_absence_claim_allowed",
                    "applied_filters", "results", "detected_extraction_gaps",
                    "truncated", "omitted_chunk_ids", "message"}
R1B_TOP_LEVEL = {"retrieval_semantics", "answer_requirements", "answer_requirements_note",
                 "candidate_source_manifest", "locator_semantics", "response_contract_revision"}


def strict_legacy_client(resp: dict) -> bool:
    """additionalProperties:false against the v2 key set — rejects unknown keys."""
    return set(resp) <= LEGACY_TOP_LEVEL


def lenient_legacy_client(resp: dict) -> bool:
    """Reads only the v2 keys it knows; ignores everything else."""
    return {"response_schema_version", "retrieval_status", "support_status",
            "results", "truncated", "omitted_chunk_ids"} <= set(resp) \
        and resp["response_schema_version"] == "v2" and isinstance(resp["results"], list)


def new_client(resp: dict) -> bool:
    """R1b-aware reader with legacy fallback: new keys optional."""
    if not lenient_legacy_client(resp):
        return False
    req = resp.get("answer_requirements", {})   # absent on a pre-R1b response — fallback
    return isinstance(req, dict)


async def r1b_checks(c) -> None:
    import sqlite3

    import rnd_factlink_mcp.server as srv
    from rnd_factlink_mcp.paths import VAULT
    from rnd_factlink_mcp.tokenize_ko import bge_token_count

    # 8. derivation + instructions bound (C1, §12 D-M)
    assert srv.mcp.instructions == srv.build_instructions(srv.ANSWER_REQUIREMENTS)
    assert len(srv.mcp.instructions) <= srv.INSTRUCTIONS_MAX_CHARS, len(srv.mcp.instructions)
    assert bge_token_count(srv.mcp.instructions) <= 600
    assert all(srv.ANSWER_REQUIREMENTS.values()), "a requirement is switched off"
    for key in srv.ANSWER_REQUIREMENTS:
        assert key in srv._REQUIREMENT_TEXT, f"requirement {key} has no sentence"
    print(f"[r1b-c1] instructions derived from requirements, {len(srv.mcp.instructions)} chars "
          f"(cap {srv.INSTRUCTIONS_MAX_CHARS}) OK")

    # 9. contract fields on search + passages + metadata; manifest integrity (C2)
    reg = srv._source_urls()
    for q in [CANARY["question"], "정부 연구개발 투자방향 및 기준의 중점 투자 분야",
              "제4차 대중교통기본계획의 비전", "재난안전 연구개발 참여 부처 현황"]:
        resp = (await c.call_tool("search_rnd_facts", {"question": q, "max_results": 8})).data
        check_v2_envelope(resp)
        assert R1B_TOP_LEVEL <= set(resp), R1B_TOP_LEVEL - set(resp)
        assert resp["retrieval_semantics"] == "nearest_candidates"
        assert resp["response_contract_revision"] == srv.CONTRACT_REVISION
        assert resp["answer_requirements"] == srv.ANSWER_REQUIREMENTS
        assert resp["answer_requirements_note"] == srv.ANSWER_REQUIREMENTS_NOTE
        # the sentences themselves live in the instructions (size bound, §12 D-A′)
        assert srv.requirements_text(srv.ANSWER_REQUIREMENTS) in srv.mcp.instructions
        assert resp["locator_semantics"] == {"pdf_page_index_base": 0, "cite_with": "citation_label"}
        man = resp["candidate_source_manifest"]
        docs_in_order = list(dict.fromkeys(r["citation"]["doc_id"] for r in resp["results"]))
        assert [m["doc_id"] for m in man] == docs_in_order, "manifest ≠ serialized results docs/order"
        for m in man:
            url = reg.get(m["doc_id"])
            assert m["source_url"] == (url if url else None), m
            assert m["source_url_status"] == ("registered" if url else "unavailable"), m
            assert set(m) == {"doc_id", "title", "source_url", "source_url_status"}, m
        for r in resp["results"]:
            cite = r["citation"]
            assert cite["variant_label"] != srv.LABEL_FALLBACK, cite["variant_type"]
            assert cite["authority_label"] != srv.LABEL_FALLBACK, cite["authority_tier"]
            if "plan" in cite:
                assert "status_label" in cite["plan"] and "statutory_label" in cite["plan"]
    chunk = resp["results"][0]["chunk_id"]
    p = (await c.call_tool("get_passages", {"chunk_ids": [chunk]})).data
    assert R1B_TOP_LEVEL - {"retrieval_semantics"} <= set(p), set(p)
    assert p["candidate_source_manifest"][0]["doc_id"] == p["passages"][0]["citation"]["doc_id"]
    meta = (await c.call_tool("get_document_metadata",
                              {"doc_id": resp["results"][0]["citation"]["doc_id"]})).data
    assert set(meta["labels"]) == {"plan_status_label", "statutory_status_label",
                                   "variant_label", "authority_label"}
    print("[r1b-c2] manifest == serialized docs, URLs = registry|null, passages/metadata carry fields OK")

    # 10. closed label mappings cover every value the served corpus can emit (C9)
    assert srv.enum_label(srv.PLAN_STATUS_LABELS, "bogus") == srv.LABEL_FALLBACK
    assert srv.enum_label(srv.PLAN_STATUS_LABELS, None) == srv.LABEL_NONE
    for d in srv._docs().values():
        for field, mapping in (("plan_status", srv.PLAN_STATUS_LABELS),
                               ("statutory_status", srv.STATUTORY_LABELS),
                               ("variant_type", srv.VARIANT_LABELS),
                               ("authority_tier", srv.AUTHORITY_LABELS)):
            v = d.get(field)
            assert v is None or v in mapping, f"{field}={v!r} on {d['doc_id']} has no label"
    print("[r1b-c9] label mappings closed and complete over the corpus OK")

    # 11. page label cases (C5): first page, range, non-PDF
    con = sqlite3.connect(VAULT / "index" / "lexical.db")
    con.row_factory = sqlite3.Row
    def row_where(sql):
        r = con.execute(f"SELECT * FROM chunks WHERE evidence_eligible = 1 AND {sql} LIMIT 1").fetchone()
        assert r is not None, sql
        rec = dict(r); rec["locator"] = __import__("json").loads(rec["locator"]); return rec
    first = srv._citation(row_where("page_start = 0 AND page_end = 0"))
    assert first["citation_label"].startswith("PDF p.1"), first["citation_label"]
    rng = srv._citation(row_where("page_start IS NOT NULL AND page_end > page_start"))
    assert "-" in rng["citation_label"].split(" ")[1], rng["citation_label"]
    non_row = con.execute("SELECT * FROM chunks WHERE evidence_eligible = 1 AND page_start IS NULL LIMIT 1").fetchone()
    if non_row is None:
        # the dev corpus currently has no citable non-PDF chunk (md summaries are tier 3);
        # exercise the label branch on a synthetic locator instead of skipping silently
        fake = dict(first_row := dict(con.execute(
            "SELECT * FROM chunks WHERE evidence_eligible = 1 LIMIT 1").fetchone()))
        fake["locator"] = {"type": "md", "heading_path": "제1장 총칙", "line_start": 1, "line_end": 9}
        fake["page_start"] = fake["page_end"] = None
        non = srv._citation(fake)
        non_note = "synthetic md locator"
    else:
        rec = dict(non_row); rec["locator"] = __import__("json").loads(rec["locator"])
        non = srv._citation(rec); non_note = "corpus"
    assert "PDF p." not in non["citation_label"], non["citation_label"]
    con.close()
    print(f"[r1b-c5] page labels: first={first['citation_label']!r} range={rng['citation_label']!r} "
          f"non-pdf={non['citation_label']!r} ({non_note}) OK")

    # 12. strict-client matrix with pre-declared expectations (§12 D-B)
    new = resp
    old = {k: v for k, v in new.items() if k not in R1B_TOP_LEVEL}   # the v2 projection
    assert new_client(old) is True, "(a) new client must accept a pre-R1b response"
    assert lenient_legacy_client(new) is True, "(b) lenient legacy client must accept R1b response"
    assert strict_legacy_client(new) is False, "(c) strict legacy client rejects by definition (expected)"
    assert strict_legacy_client(old) is True
    print("[r1b-strict] (a) pass (b) pass (c) expected rejection — matrix as declared OK")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
