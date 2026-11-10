"""rnd-factlink MCP server — response contract v2 (docs/SCHEMA.md §7).

v2 (2026-08-09, 3-round adversarial review):
- no support judgment: retrieval_status is a mechanical invariant only,
  support_status is always "not_assessed" (server never promotes similarity
  to "evidence found")
- results contain citable chunks only (eligibility filtered before candidate
  cutoffs in search_hybrid); personal summaries (tier 3) are not returned
- raw scores are never exposed in responses (query log / dev CLI only)
- truncation is always explicit (excerpt_truncated / text_complete / truncated)
- lexical-dense snapshot consistency is verified fail-closed at first search
This contract is an interim safety measure, not the finished product promise;
a conservative server-side support assessor is a post-golden-set task.

R1b (2026-09, UPDATE-PLAN-R1 §3/§11/§12) — additive revision of v2:
- answer_requirements (booleans) + answer_requirements_text derived by one function;
  the same text is the connector instructions (C1)
- candidate_source_manifest built from the serialized results, null URLs explicit (C2)
- retrieval_semantics="nearest_candidates" constant (C4), locator_semantics (C5)
- natural-language labels beside every enumerated field (C9)
- adjacent-figure substitution rule and scoped not-found wording (C10)
Retrieval itself is untouched — scripts/test_r1b_invariance.py proves it bit-for-bit.
"""

import json
import time
from datetime import datetime, timezone

from fastmcp import FastMCP

from .embed_query import EmbedUnavailable
from .paths import REPO_ROOT, VAULT
from .search_hybrid import (LexicalUnavailable, extraction_gaps, hybrid_search,
                            verify_snapshot_consistency)
from .tokenize_ko import bge_token_count

SCHEMA_VERSION = "v2"
# R1b (UPDATE-PLAN-R1 §3/§12): the v2 contract gains fields, never loses or
# retypes one. Strict clients that reject unknown keys will refuse the new
# response by definition (an announced additive change of the public alpha);
# lenient clients ignore the additions.
CONTRACT_REVISION = "2026-09-r1b"
RETRIEVAL_SEMANTICS = "nearest_candidates"   # C4 — never a support judgment, no threshold
LOCATOR_SEMANTICS = {"pdf_page_index_base": 0, "cite_with": "citation_label"}  # C5
EXCERPT_MAX_TOKENS = 450
DEFAULT_BUDGET_TOKENS = 4000
HARD_CAP_TOKENS = 8000
# Regression bound for the connector instructions (§12 D-M): the sealed R1b text
# measured 1,079 chars / 524 BGE tokens (was 780 / 379 before R1b). Growth past this is a review item, not
# a protocol limit — the per-call rules live in answer_requirements instead.
INSTRUCTIONS_MAX_CHARS = 1200
QUERY_LOG = VAULT / "derived" / "reports" / "query_log.jsonl"
# Ships with the repo and the public bundle (unlike the Gate 0 baseline, which
# lives beside the private originals) — never derive this from BASELINE_PATH.
SOURCE_URLS_PATH = REPO_ROOT / "curation" / "source_urls.jsonl"

NO_CANDIDATES_MESSAGE = ("검색 후보를 반환하지 못했습니다. "
                         "색인된 코퍼스에 근거가 없다는 판정은 아닙니다.")

# ---------------------------------------------------------------------------
# C1 — answer requirements. The booleans are the contract; the natural-language
# sentences are DERIVED from them by one function, so the two can never drift
# (a copied constant would fail silently). The same dict rides in every search
# and passage response, and the same derived text forms the connector
# instructions — one source, three surfaces.
# ---------------------------------------------------------------------------
ANSWER_REQUIREMENTS = {
    "direct_support_check": True,
    "scoped_not_found_only": True,
    "no_adjacent_substitution": True,
    "cite_used_source_urls_only": True,
    "mark_missing_source_url": True,
    "cite_pages_with_label": True,
    "plain_language_for_enums": True,
}

_REQUIREMENT_TEXT = {
    "direct_support_check":
        "passage가 주장을 직접 뒷받침하고 범위·시점·정의·단위가 일치할 때만 근거로 쓴다.",
    "scoped_not_found_only":
        "요청한 값을 확인하지 못하면 \"반환된 후보에서는 요청하신 <문서>의 <연도>·<지표> 값을 "
        "직접 확인하지 못했습니다\"라고만 말한다. 코퍼스 전체나 실제 세계에 없다고 선언하지 "
        "않고, 코퍼스 밖 문서의 존재도 단정하지 않는다.",
    "no_adjacent_substitution":
        "질문이 특정 문서·연도·지표의 값을 묻는데 후보에 그 값이 없으면, 다른 문서나 다른 "
        "연도의 비슷한 수치로 대체해 답하지 않는다. 관련 수치를 덧붙일 때는 참고 정보임과 어느 "
        "문서의 어느 연도 값인지를 함께 적는다.",
    "cite_used_source_urls_only":
        "답변 하단에 실제로 인용한 문서의 source_url만 목록으로 적는다. "
        "candidate_source_manifest는 후보 목록이므로 통째로 복사하지 말고, 인용하지 않은 "
        "문서의 URL은 적지 않는다.",
    "mark_missing_source_url":
        "source_url이 null인 문서는 URL을 추정·생성하거나 웹 검색 결과로 대체하지 않고 "
        "\"원문 URL 미확보\"로 표기한다.",
    "cite_pages_with_label":
        "쪽수는 citation_label 문구를 그대로 쓴다. locator의 page_start·page_end는 0부터 세는 "
        "내부 색인이므로 답변에 쓰지 않는다.",
    "plain_language_for_enums":
        "plan_status·statutory_status·variant_type·authority_tier 같은 필드의 원값(current, "
        "superseded 등)을 답변에 그대로 쓰지 않는다. 판단에는 사용하되 문장에서는 함께 제공되는 "
        "status_label·variant_label 등의 자연어를 쓴다.",
}


def requirements_text(req: dict) -> str:
    """The one place the requirement sentences are produced (C1)."""
    return " ".join(_REQUIREMENT_TEXT[k] for k, v in req.items() if v)


def build_instructions(req: dict) -> str:
    """Connector instructions = invariant framing + the derived requirement
    sentences + the web-boundary clause kept as a separate, advisory section (C3)."""
    head = ("한국 정부 R&D 정책문서·보고서(등재된 코퍼스)에서 검색 후보 passage를 반환한다. "
            "결과는 가장 가까운 후보(retrieval_semantics=nearest_candidates)이며, 서버는 "
            "직접지지 여부를 판정하지 않는다(support_status=not_assessed). excerpt가 절단됐거나 "
            "판단에 부족하면 get_passages로 전문을 확인한다. ")
    body = "답변 작성 규칙(answer_requirements): " + requirements_text(req)
    web = (" 웹 경계(권고): 코퍼스 후보에서 확인한 내용과 그 밖에서 아는 내용을 구분해 쓴다. "
           "passage의 향후 일정·추진계획은 예정 사항이며 산출물이 생성됐다는 근거가 아니다.")
    return head + body + web


# ---------------------------------------------------------------------------
# C9 — natural-language labels for enumerated fields. Raw values stay for
# machine consumers; the labels are what the model should put in a sentence.
# Closed mappings: an unlisted value gets the safe fallback, never the raw
# string, and a missing (null) value is never translated into "non-plan" or
# "non-statutory" — null means "no curated information".
# ---------------------------------------------------------------------------
PLAN_STATUS_LABELS = {"current": "현행 계획", "superseded": "후속 계획으로 대체된 구판",
                      "expired": "계획기간 종료", "unknown": "현행성 미확인"}
STATUTORY_LABELS = {"statutory": "법정계획", "non_statutory": "비법정계획",
                    "unknown": "법정 여부 미확인"}
VARIANT_LABELS = {"original": "원문", "full_text": "전문", "summary": "요약본",
                  "press_release": "보도자료", "notice": "고시문", "excerpt": "발췌"}
AUTHORITY_LABELS = {1: "원문·고시(1차 출처)", 2: "공식 보도자료(2차 출처)",
                    3: "운영자 작성 요약(파생 자료)"}
LABEL_FALLBACK = "상태 미확인"
LABEL_NONE = "등재 정보 없음"


def enum_label(mapping: dict, value) -> str:
    if value is None:
        return LABEL_NONE
    return mapping.get(value, LABEL_FALLBACK)


mcp = FastMCP(
    "korean-rnd-mcp",
    mask_error_details=True,  # 예상 오류는 error dict로 반환 — 내부 예외 상세는 미노출
    instructions=build_instructions(ANSWER_REQUIREMENTS))

_docs_cache: dict | None = None
_pages_cache: dict | None = None
_source_urls_cache: dict | None = None
# HTTP(공개) 모드에서는 False — raw 질의문·intent·점수를 저장하지 않는다 (결정 #10)
_raw_query_logging = True


# Each cache is built in a local and published with a single assignment. The
# boot warm-up fills these from a worker thread while FastMCP may already be
# running tool calls in its own threadpool; publishing an empty dict first and
# filling it afterwards would let a concurrent reader see a half-loaded corpus
# and raise KeyError on a valid doc_id.
def _source_urls() -> dict:
    """doc_id -> source_url. curation/source_urls.jsonl (git-tracked, may be empty)."""
    global _source_urls_cache
    if _source_urls_cache is None:
        urls: dict = {}
        if SOURCE_URLS_PATH.exists():
            for line in SOURCE_URLS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("doc_id") and rec.get("source_url"):
                    urls[rec["doc_id"]] = rec["source_url"]
        _source_urls_cache = urls
    return _source_urls_cache


def _docs() -> dict:
    global _docs_cache
    if _docs_cache is None:
        docs: dict = {}
        for line in (VAULT / "meta" / "documents.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            docs[d["doc_id"]] = d
        _docs_cache = docs
    return _docs_cache


def _pages() -> dict:
    global _pages_cache
    if _pages_cache is None:
        pages: dict = {}
        for line in (VAULT / "meta" / "pages.jsonl").read_text(encoding="utf-8").splitlines():
            p = json.loads(line)
            pages[(p["doc_id"], p["pdf_page_index"])] = p
        _pages_cache = pages
    return _pages_cache


def _citation(r: dict) -> dict:
    doc = _docs()[r["doc_id"]]
    cite = {
        "doc_id": r["doc_id"],
        "title": doc["title"],
        "institution": doc.get("institution_normalized"),
        "filename_date": doc.get("filename_date"),
        "variant_type": r["variant_type"],
        "authority_tier": r["authority_tier"],
        "citation_allowed": bool(r["evidence_eligible"]),
        "locator": r["locator"],
        # C9 labels — additive, derived from the raw values above
        "variant_label": enum_label(VARIANT_LABELS, r["variant_type"]),
        "authority_label": enum_label(AUTHORITY_LABELS, r["authority_tier"]),
    }
    url = _source_urls().get(r["doc_id"])
    if url:
        cite["source_url"] = url
    # curated plan metadata, shown only when this document is a registered plan.
    # Short fields only — current_basis etc. live in get_document_metadata.
    if doc.get("plan_id"):
        cite["plan"] = {
            "plan_id": doc["plan_id"],
            "plan_status": doc.get("plan_status"),
            "plan_status_as_of": doc.get("plan_status_as_of"),
            "statutory_status": doc.get("statutory_status"),
            "plan_period": doc.get("plan_period"),
            "status_label": enum_label(PLAN_STATUS_LABELS, doc.get("plan_status")),
            "statutory_label": enum_label(STATUTORY_LABELS, doc.get("statutory_status")),
        }
    if r["page_start"] is not None:
        pdf_part = f"PDF p.{r['page_start'] + 1}"
        if r["page_end"] != r["page_start"]:
            pdf_part += f"-{r['page_end'] + 1}"
        printed = []
        for idx in (r["page_start"], r["page_end"]):
            p = _pages().get((r["doc_id"], idx))
            if p and p["printed_page_number"] and (p["printed_page_confidence"] or 0) >= 0.8:
                printed.append(str(p["printed_page_number"]))
        cite["citation_label"] = pdf_part + (f" (인쇄면 {'-'.join(dict.fromkeys(printed))})" if printed else "")
    else:
        loc = r["locator"]
        cite["citation_label"] = loc.get("sheet") and f"시트 '{loc['sheet']}'" or \
            (loc.get("heading_path") and f"섹션 '{loc['heading_path']}'") or "문서 본문"
    return cite


def _manifest(citations: list[dict]) -> list[dict]:
    """C2 — candidate_source_manifest, built from the SERIALIZED citations (so a
    result dropped by the token budget never appears here), one entry per
    document in order of first appearance. source_url is the registry value or
    an explicit null; the status field is the flag the null-URL rule keys on.
    Kept deliberately slim (doc_id, title, url, status): institution and page
    labels already ride in each citation, and at max_results=8 the URLs alone
    are the bulk of the entry — see scripts/test_r1b_size.py for the bound."""
    order: list[str] = []
    entries: dict[str, dict] = {}
    for c in citations:
        d = c["doc_id"]
        if d not in entries:
            order.append(d)
            url = c.get("source_url")
            entries[d] = {"doc_id": d, "title": c["title"],
                          "source_url": url if url else None,
                          "source_url_status": "registered" if url else "unavailable"}
    return [entries[d] for d in order]


ANSWER_REQUIREMENTS_NOTE = ("규칙 원문은 서버 instructions의 답변 작성 규칙 참조. 특히: 인용한 문서의 "
                            "source_url만 하단 목록으로, null이면 \"원문 URL 미확보\" 표기, "
                            "후보에 없는 값은 인접 수치로 대체하지 말고 확인 불가로.")


def _contract_fields() -> dict:
    """Fields every R1b response carries (search and passages alike).

    The requirement BOOLEANS ride in every response; the derived sentences live
    in the connector instructions only. Measured reason (test_r1b_size.py): at
    max_results=8 the pre-R1b worst case already used 6,923 of the 8,000-token
    budget, and repeating the 375-token sentence block per response pushed the
    total to 8,327. A one-line pointer keeps the per-call reminder at ~40 tokens."""
    return {
        "response_contract_revision": CONTRACT_REVISION,
        "answer_requirements": dict(ANSWER_REQUIREMENTS),
        "answer_requirements_note": ANSWER_REQUIREMENTS_NOTE,
        "locator_semantics": dict(LOCATOR_SEMANTICS),
    }


def url_registry_diagnostics() -> dict:
    """Operator diagnostics for the readiness file (never a public response):
    registry digest, counts, and the digest of the served-docs-without-URL set.
    The approval value for that set lives in curation/expected_null_source_urls.json
    and is checked by the bundle gate — this only reports what the process sees."""
    import hashlib
    reg_sha = hashlib.sha256(SOURCE_URLS_PATH.read_bytes()).hexdigest() \
        if SOURCE_URLS_PATH.exists() else None
    nulls = sorted(d for d in _docs() if d not in _source_urls())
    return {"url_registry_sha256": reg_sha, "n_source_urls": len(_source_urls()),
            "n_documents": len(_docs()), "n_null_source_url_docs": len(nulls),
            "null_source_url_set_sha256": hashlib.sha256("\n".join(nulls).encode()).hexdigest()}


def _excerpt(text: str, max_tokens: int = EXCERPT_MAX_TOKENS) -> tuple[str, int, bool]:
    """Returns (excerpt, token_count, truncated)."""
    n = bge_token_count(text)
    if n <= max_tokens:
        return text, n, False
    lines = text.splitlines()
    out: list[str] = []
    total = 0
    for line in lines:
        t = bge_token_count(line)
        if total + t > max_tokens and out:
            break
        out.append(line)
        total += t
    return "\n".join(out), total, True


def _len_bucket(text: str) -> str:
    n = len(text)
    return "0-50" if n <= 50 else "51-100" if n <= 100 else \
        "101-200" if n <= 200 else "201-500"


def _log_query(question: str, intent: str | None, retrieval_status: str,
               rows: list[dict], snapshot_id: str, latency_ms: int) -> None:
    """Internal log. Dev(stdio) keeps raw scores for threshold calibration;
    public(HTTP) mode records only counts/status/latency/length bucket —
    never the query text (decision #10). No passage text is ever logged."""
    QUERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    # size-capped rotation (single .1 generation) — an append-only log on a
    # small serving-host volume must not grow unbounded
    try:
        if QUERY_LOG.exists() and QUERY_LOG.stat().st_size > 10 * 1024 * 1024:
            QUERY_LOG.replace(QUERY_LOG.with_suffix(".jsonl.1"))
    except OSError:
        pass
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema": SCHEMA_VERSION,
        "index_snapshot_id": snapshot_id,
        "retrieval_status": retrieval_status,
        "n_results": len(rows),
        "latency_ms": latency_ms,
    }
    if _raw_query_logging:
        rec.update({
            "question": question, "intent": intent,
            "results": [{"chunk_id": r["chunk_id"], "rrf": r["rrf_score"],
                         "sparse_rank": r["sparse_rank"], "dense_rank": r["dense_rank"],
                         "dense_cosine": r["dense_score"]} for r in rows],
        })
    else:
        rec["question_len_bucket"] = _len_bucket(question)
    with open(QUERY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


@mcp.tool
def search_rnd_facts(question: str, intent: str | None = None, max_results: int = 5,
                     plan_status_filter: str = "all") -> dict:
    """정부 R&D 정책문서·보고서(등재된 코퍼스)에서 질문 관련 검색 후보 passage를 반환한다.

    반환되는 것은 검색 후보다 — 서버는 직접 뒷받침 여부를 판정하지 않는다
    (support_status는 항상 not_assessed). 호출자가 passage 내용이 질문의 주장을 직접
    뒷받침하는지(범위·시점·정의·단위 일치) 확인해야 한다. detected_extraction_gaps가
    비어 있어도 코퍼스가 완전하다는 뜻이 아니다. 개인 작성 요약은 반환되지 않는다.

    Args:
        question: 검색할 질문(한국어 자연문, 500자 이내). 필수.
        intent: 궁극적으로 알고 싶은 것(선택). 기록용 — 검색에는 사용되지 않음.
        max_results: 반환 후보 수(1~8, 기본 5).
        plan_status_filter: "all"(기본) 또는 "current". "current"는 현행으로 확인된
            정부 계획 문서만 검색한다(상위계획 부합성 근거 탐색용). 미큐레이션·비계획
            문서와 구판은 제외되므로, 과거 시점 질의나 일반 검색에는 "all"을 유지할 것.
            질문에 "현재"가 있다고 자동 적용되지 않는다 — 명시적으로 선택해야 한다.
    """
    question = (question or "").strip()
    if not question:
        return {"error": "question이 비어 있습니다"}
    if len(question) > 500:
        return {"error": "question은 500자 이내여야 합니다"}
    if plan_status_filter not in ("all", "current"):
        return {"error": "plan_status_filter는 'all' 또는 'current'여야 합니다"}
    max_results = max(1, min(int(max_results), 8))

    t0 = time.perf_counter()
    snapshot_id = verify_snapshot_consistency()
    try:
        rows = hybrid_search(question, k=max_results, plan_status_filter=plan_status_filter)
    except EmbedUnavailable as e:
        # fail-closed: no silent BM25-only degradation (decision #8/#11)
        _log_query(question, intent, "embed_unavailable", [], snapshot_id,
                   int((time.perf_counter() - t0) * 1000))
        return {"error": f"검색 불가(질의 임베딩 실패, fail-closed): {e}",
                "retryable": True}
    except LexicalUnavailable as e:
        # symmetric with the embedding leg: no dense-only degradation. str(e) is
        # the sqlite3 error class name only — never a message that could quote
        # the question (decision #10).
        _log_query(question, intent, "lexical_unavailable", [], snapshot_id,
                   int((time.perf_counter() - t0) * 1000))
        return {"error": f"검색 불가(어휘 색인 오류, fail-closed): {e}",
                "retryable": True}
    retrieval_status = "candidates_returned" if rows else "no_candidates"

    results, total_tokens, truncated, omitted = [], 0, False, []
    for r in rows:
        excerpt, n, cut = _excerpt(r["text_raw"])
        if total_tokens + n > DEFAULT_BUDGET_TOKENS and results:
            truncated = True
            omitted.append(r["chunk_id"])
            continue
        total_tokens += n
        results.append({
            "chunk_id": r["chunk_id"],
            "excerpt": excerpt,
            "excerpt_truncated": cut,
            "citation": _citation(r),
        })

    _log_query(question, intent, retrieval_status, rows, snapshot_id,
               int((time.perf_counter() - t0) * 1000))
    resp = {
        "response_schema_version": SCHEMA_VERSION,
        "retrieval_status": retrieval_status,
        "support_status": "not_assessed",
        "direct_answer_check_required": True,
        "corpus_absence_claim_allowed": False,
        "applied_filters": {"plan_status_filter": plan_status_filter},
        "results": results,
        "detected_extraction_gaps": extraction_gaps(question),
        "truncated": truncated,
        "omitted_chunk_ids": omitted,
        # ---- R1b additions (v2 stays; see CONTRACT_REVISION) ----
        "retrieval_semantics": RETRIEVAL_SEMANTICS,
        "candidate_source_manifest": _manifest([r["citation"] for r in results]),
        **_contract_fields(),
    }
    if plan_status_filter == "current" and not results:
        resp["message"] = ("현행 확인 계획 문서 범위에서 후보를 찾지 못했습니다. "
                          "코퍼스 전체 검색은 plan_status_filter='all'로 재시도하십시오.")
    if retrieval_status == "no_candidates":
        resp["message"] = NO_CANDIDATES_MESSAGE
    return resp


@mcp.tool
def get_passages(chunk_ids: list[str], max_chars_per_passage: int = 6000) -> dict:
    """search_rnd_facts가 반환한 chunk_id의 전체 passage 텍스트를 조회한다.

    "전체"는 해당 passage(청크)의 전체 텍스트를 뜻한다(문서 전체 아님).
    text_complete=false면 상한으로 절단된 것이므로 그 사실을 반영해 판단하라.

    Args:
        chunk_ids: 조회할 chunk_id 목록(최대 8개).
        max_chars_per_passage: passage당 문자 상한(기본 6000).
    """
    if not chunk_ids:
        return {"error": "chunk_ids가 비어 있습니다"}
    if len(chunk_ids) > 8:
        return {"error": "chunk_ids는 최대 8개입니다"}
    verify_snapshot_consistency()
    import sqlite3
    con = sqlite3.connect(VAULT / "index" / "lexical.db")
    con.row_factory = sqlite3.Row
    out, total_tokens = [], 0
    for cid in chunk_ids:
        row = con.execute("SELECT * FROM chunks WHERE chunk_id = ?", (str(cid),)).fetchone()
        # runtime allowlist (defense in depth behind the physically filtered
        # public index): the chunk's document must be a served document and the
        # chunk citable. One message for both cases — existence is not leaked.
        if row is None or row["doc_id"] not in _docs() or not row["evidence_eligible"]:
            out.append({"chunk_id": cid, "error": "존재하지 않거나 제공 대상이 아닌 chunk_id"})
            continue
        r = dict(row)
        r["locator"] = json.loads(r["locator"])
        limit = max(200, int(max_chars_per_passage))
        text = r["text_raw"][:limit]
        complete = len(text) == len(r["text_raw"])
        n = bge_token_count(text)
        if total_tokens + n > HARD_CAP_TOKENS:
            out.append({"chunk_id": cid, "error": "토큰 예산 초과로 생략(개별 재요청 필요)"})
            continue
        total_tokens += n
        out.append({"chunk_id": cid, "text": text, "text_complete": complete,
                    "citation": _citation(r)})
    con.close()
    return {"response_schema_version": SCHEMA_VERSION, "passages": out,
            "candidate_source_manifest": _manifest([p["citation"] for p in out if "citation" in p]),
            **_contract_fields()}


@mcp.tool
def get_document_metadata(doc_id: str) -> dict:
    """문서의 메타데이터(발간정보·변형 관계·페이지 품질 요약)를 조회한다.

    같은 family의 개인 작성 요약(검색 응답에서는 제외됨) 존재 여부도 여기서 확인
    가능하다. 단 공식 문서와 family로 연결되지 않은 요약 전용 문서는 검색에서
    의도적으로 발견되지 않는다.

    Args:
        doc_id: documents 식별자(d_ 접두).
    """
    verify_snapshot_consistency()
    doc = _docs().get(str(doc_id))
    if doc is None:
        return {"error": f"존재하지 않는 doc_id: {doc_id}"}
    fam_members = [
        {"doc_id": d["doc_id"], "title": d["title"], "variant_type": d["variant_type"],
         "authority_tier": d["authority_tier"], "evidence_eligible": d["evidence_eligible"],
         "relation_type": d.get("relation_type")}
        for d in _docs().values() if d.get("family_id") == doc.get("family_id")
    ] if doc.get("family_id") else []
    keep = ["doc_id", "title", "institution_raw", "institution_normalized", "filename_date",
            "date_semantics", "variant_type", "authority_tier", "evidence_eligible",
            "family_id", "relation_type", "relation_target", "source_folder", "ext",
            "n_pages", "page_status_counts", "servable_scope", "rights_review_status",
            "metadata_parse_status",
            # curated plan metadata (null = 미큐레이션, "비계획으로 확인됨"이 아님)
            "plan_id", "plan_ids", "plan_status", "plan_status_as_of", "current_basis",
            "statutory_status", "statutory_basis", "plan_period", "announced_on",
            "plan_ministry", "rnd_level"]
    resp = {"response_schema_version": SCHEMA_VERSION,
            "response_contract_revision": CONTRACT_REVISION,
            **{k: doc.get(k) for k in keep}, "family_members": fam_members,
            "filename_date_note": "파일명 유래 날짜 — 발간일로 단정 금지",
            # C9 — natural-language labels for the enumerated fields above
            "labels": {
                "plan_status_label": enum_label(PLAN_STATUS_LABELS, doc.get("plan_status")),
                "statutory_status_label": enum_label(STATUTORY_LABELS, doc.get("statutory_status")),
                "variant_label": enum_label(VARIANT_LABELS, doc.get("variant_type")),
                "authority_label": enum_label(AUTHORITY_LABELS, doc.get("authority_tier")),
            }}
    url = _source_urls().get(doc["doc_id"])
    if url:
        resp["source_url"] = url
    return resp


# Fixed warm-up query. Must tokenize to a non-empty Kiwi token set, otherwise
# bm25_ranking returns before touching SQLite and the FTS statistics stay cold.
WARM_QUERY = "국가연구개발사업 예산 투자 방향"
# Uvicorn's graceful-shutdown budget. FastMCP hardcodes 2 seconds unless the
# caller overrides it (fastmcp/server/mixins/transport.py), which would cancel an
# in-flight search — a query waiting on the concurrency slot (up to 5s) and then
# on a Workers AI call (5s connect + 15s read) can legitimately outlive that.
# Keep this below the compose stop_grace_period so Docker never SIGKILLs first.
GRACEFUL_SHUTDOWN_S = 25


def _warm_pass(stage) -> None:
    """Load every process-level cache a first query would otherwise pay for.

    Runs in a worker thread (readiness.start_warmup). Local paths only: the
    remote query embedding is deliberately excluded, so this spends no Workers AI
    neurons and writes no query-log record. `stage` records progress for the
    diagnostics file — it never reaches a public response.
    """
    import sqlite3

    from . import readiness, search_hybrid

    stage("snapshot")
    verify_snapshot_consistency()  # already done in preflight; cheap re-assert
    stage("metadata")
    _docs()
    _pages()
    _source_urls()
    stage("retrieval")
    top_chunk = search_hybrid.warm_local(
        WARM_QUERY, readiness.deterministic_warm_vector(search_hybrid.dense_dim()))
    stage("passage")
    if top_chunk:
        con = sqlite3.connect(VAULT / "index" / "lexical.db")
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM chunks WHERE chunk_id = ?", (top_chunk,)).fetchone()
        con.close()
        if row is not None:
            r = dict(row)
            r["locator"] = json.loads(r["locator"])
            _citation(r)              # warms the pages/source-url lookups
            _excerpt(r["text_raw"])   # warms the HF tokenizer on real passage text
    stage("gaps")
    extraction_gaps(WARM_QUERY)


def _run_http() -> None:
    """Streamable HTTP mode for the public deployment (Docker on the serving host behind a
    Cloudflare Tunnel). Binds 127.0.0.1 by default; the container sets
    RND_FACTLINK_HTTP_HOST=0.0.0.0. Raw query logging is forced off.

    Startup preflight: the index snapshot check runs BEFORE serving, so a bad
    bundle fails the boot instead of answering metadata/passage calls while
    search would fail (review 2026-08-21 P0). Stateless HTTP + JSON responses:
    the tools keep no session state, and long-lived SSE streams would sit
    outside the POST concurrency guard.

    After the socket is bound, the warm pass runs in the background and the
    guard answers MCP requests 503 until it finishes (UPDATE-PLAN-R1 §1 C6').
    Nothing before the bind is observable from outside: /livez only starts
    answering once uvicorn is listening, so the "starting" state is internal."""
    global _raw_query_logging
    _raw_query_logging = False
    import asyncio
    import os

    from starlette.middleware import Middleware

    from . import readiness
    from .http_guard import AbuseGuard, guard_config_from_env

    snapshot_id = verify_snapshot_consistency()  # raises on mismatch — boot fails
    readiness.set_diagnostics(**url_registry_diagnostics())  # tmpfs file only (§12 D-H)
    print(f"preflight OK: index snapshot {snapshot_id}, "
          f"{len(_docs())} documents, release {readiness.release_id()}", flush=True)

    host = os.environ.get("RND_FACTLINK_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8081"))
    kwargs = {}
    allowed = os.environ.get("RND_FACTLINK_ALLOWED_HOSTS")
    if allowed:  # e.g. "korean-rnd-mcp.rndmanagers.org,127.0.0.1,localhost"
        kwargs["allowed_hosts"] = [h.strip() for h in allowed.split(",") if h.strip()]

    async def _serve() -> None:
        readiness.start_warmup(_warm_pass)
        await mcp.run_http_async(
            transport="streamable-http", host=host, port=port,
            stateless_http=True, json_response=True,
            middleware=[Middleware(AbuseGuard, **guard_config_from_env())],
            uvicorn_config={"access_log": False,  # 질의 URL·IP를 access log에 남기지 않음
                            "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_S},
            **kwargs,
        )

    asyncio.run(_serve())


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog="korean-rnd-mcp",
        description="정부 R&D 출처·인용 근거 검색 MCP 서버. 기본은 stdio, --http는 공개 배포용.")
    parser.add_argument("--http", action="store_true",
                        help="Streamable HTTP 모드로 실행 (PORT 환경변수, 기본 8081)")
    args = parser.parse_args()
    if args.http:
        _run_http()
    else:
        mcp.run()


if __name__ == "__main__":
    main()
