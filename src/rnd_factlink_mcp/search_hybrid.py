"""Hybrid retrieval (v2 contract): BM25 (kiwi/FTS5) + dense (BGE-M3) fused by RRF.

v2 contract points implemented here (docs/SCHEMA.md §7):
- eligibility (citation_allowed / evidence_eligible) is enforced BEFORE each
  retrieval leg's candidate cutoff — ineligible chunks never consume the
  candidate window, family cap, or k slots
- no support/threshold judgment: scores are internal (dev CLI / query log only)
- extraction gaps are structured and use the canonical GAP_PAGE_STATUS set
- lexical and dense snapshots must agree (fail-closed check)
"""

import hashlib
import json
import sqlite3

from . import tokenize_ko
from .embed_dense import DenseIndex
from .page_status import GAP_PAGE_STATUS
from .paths import VAULT

RRF_K = 60
CANDIDATES = 50
# Candidate-window expansion (see docs/SCHEMA.md §10). A long document can fill
# a leg's whole candidate window, so the post-fusion family cap then has nothing
# left to promote and fewer than k results come back. Measured 2026-08-13:
# "건설 분야 기술수준" filled 47/50 BM25 candidates from one document and returned
# 4 results for k=8; at depth 100 the same query has capacity for 9. We therefore
# keep the depth-50 behaviour untouched and only go deeper when the cap starves
# the result set. Ranks stay raw (a deeper window is a longer prefix of the same
# ranking), so RRF contributions are never inflated by re-numbering.
EXPANSION_FACTOR = 3
MAX_CANDIDATES = 2000

PLAN_STATUS_FILTERS = ("all", "current")


class LexicalUnavailable(RuntimeError):
    """The lexical (FTS5) leg could not run — fail closed, like EmbedUnavailable.

    Carries only the sqlite3 error class name: the original message can quote
    query tokens ("no such column: 미"), and the HTTP mode never records query
    text (decision #10). Raised `from None`, so a traceback prints neither the
    original exception nor its message. No dense-only degradation: a silent
    half-search would be an unannounced quality loss on a citation service."""

_dense: DenseIndex | None = None
_eligible_mask = None  # np.bool_ array aligned with DenseIndex.ids
_current_mask = None   # np.bool_ array — True where chunk's plan_status='current'
_snapshot_id: str | None = None
_chunk_meta: dict[str, tuple[str, str, int]] | None = None  # chunk_id -> (family, doc, eligible)
_gap_candidate_cache: list[dict] | None = None


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(VAULT / "index" / "lexical.db")
    con.row_factory = sqlite3.Row
    return con


def _lexical_chunk_ids() -> list[str]:
    con = _con()
    ids = [r["chunk_id"] for r in con.execute("SELECT chunk_id FROM chunks ORDER BY rowid")]
    con.close()
    return ids


def verify_snapshot_consistency() -> str:
    """Fail-closed check that lexical and dense indexes describe the same corpus
    snapshot. Returns the snapshot id (sha256 prefix of the chunk-id list)."""
    global _snapshot_id
    if _snapshot_id is not None:
        return _snapshot_id
    lex_ids = _lexical_chunk_ids()
    dense_ids = _dense_index().ids
    lex_fp = hashlib.sha256("\n".join(lex_ids).encode()).hexdigest()
    dense_fp = hashlib.sha256("\n".join(dense_ids).encode()).hexdigest()
    if lex_fp != dense_fp:
        raise RuntimeError(
            f"index snapshot mismatch: lexical {lex_fp[:12]} vs dense {dense_fp[:12]} — "
            "rebuild with scripts/build_index.py then scripts/build_dense.py")
    _snapshot_id = lex_fp[:12]
    return _snapshot_id


def _dense_index() -> DenseIndex:
    global _dense
    if _dense is None:
        _dense = DenseIndex()
    return _dense


def _eligibility_mask():
    """Boolean mask aligned with DenseIndex.ids — True where chunk is citable."""
    global _eligible_mask
    if _eligible_mask is None:
        import numpy as np
        con = _con()
        eligible = {r["chunk_id"] for r in con.execute(
            "SELECT chunk_id FROM chunks WHERE evidence_eligible = 1")}
        con.close()
        _eligible_mask = np.array([cid in eligible for cid in _dense_index().ids], dtype=bool)
    return _eligible_mask


def _chunk_metadata() -> dict[str, tuple[str, str, int]]:
    """chunk_id -> (family_key, doc_id, evidence_eligible), cached for the process.
    Avoids one SQLite round-trip per candidate while walking the fused ranking."""
    global _chunk_meta
    if _chunk_meta is None:
        con = _con()
        _chunk_meta = {
            r["chunk_id"]: (r["family_id"] or r["doc_id"], r["doc_id"], int(r["evidence_eligible"]))
            for r in con.execute(
                "SELECT chunk_id, family_id, doc_id, evidence_eligible FROM chunks")
        }
        con.close()
    return _chunk_meta


def _search_mask(plan_status_filter: str):
    """Combined eligibility (+ optional plan-status) mask for the dense leg.
    Filters must act BEFORE each leg's cutoff (SCHEMA §10/§11) — removing rows
    after fusion would let excluded chunks occupy the candidate window."""
    base = _eligibility_mask()
    if plan_status_filter == "all":
        return base
    global _current_mask
    if _current_mask is None:
        import numpy as np
        con = _con()
        current = {r["chunk_id"] for r in con.execute(
            "SELECT chunk_id FROM chunks WHERE plan_status = 'current'")}
        con.close()
        _current_mask = np.array([cid in current for cid in _dense_index().ids], dtype=bool)
    return base & _current_mask


def bm25_ranking(query: str, k: int = CANDIDATES,
                 plan_status_filter: str = "all") -> list[str]:
    """BM25 leg — eligibility (and plan-status) filtered BEFORE the LIMIT cutoff."""
    match = tokenize_ko.fts_match_query(query)   # quoted tokens (S1 fix)
    if not match:
        return []
    plan_clause = " AND c.plan_status = 'current'" if plan_status_filter == "current" else ""
    # Everything sqlite — open, MATCH, fetch, close — sits inside one boundary:
    # any sqlite3.Error becomes a fixed LexicalUnavailable (class name only),
    # and the connection is closed on every path.
    try:
        con = _con()
        try:
            rows = con.execute(
                "SELECT c.chunk_id FROM fts_kiwi JOIN chunks c ON c.rowid = fts_kiwi.rowid"
                " WHERE fts_kiwi MATCH ? AND c.evidence_eligible = 1" + plan_clause +
                " ORDER BY bm25(fts_kiwi, 3.0, 2.0, 1.0), c.chunk_id LIMIT ?",
                (match, k)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise LexicalUnavailable(type(e).__name__) from None
    return [r["chunk_id"] for r in rows]


def rank_precomputed_embedding(query_vector, plan_status_filter: str = "all"
                               ) -> list[tuple[str, float]]:
    """Whole eligible ranking for an ALREADY-computed query vector.

    Split out of _dense_ranking_full so the boot warm-up can drive the exact
    production matrix path (mmap page-in, eligibility mask, stable argsort) with
    a local vector. Warming through _dense_ranking_full would embed a query
    remotely and spend Workers AI neurons on a warm-up (UPDATE-PLAN-R1 §1 C6').
    """
    import numpy as np
    idx = _dense_index()
    qv = np.asarray(query_vector, dtype=np.float32)
    dim = idx.vecs.shape[1]
    if qv.shape != (dim,) or not np.all(np.isfinite(qv)):
        raise ValueError(f"query vector invalid: shape {qv.shape}, expected ({dim},)")
    scores = idx.vecs @ qv
    scores = np.where(_search_mask(plan_status_filter), scores, -np.inf)
    order = np.argsort(-scores, kind="stable")
    return [(idx.ids[i], float(scores[i])) for i in order if scores[i] > -np.inf]


def _dense_ranking_full(query: str, plan_status_filter: str = "all") -> list[tuple[str, float]]:
    """Whole eligible ranking, scored and sorted once. Expansion slices this list
    instead of re-embedding the query for every round."""
    from .embed_query import embed_query
    return rank_precomputed_embedding(embed_query(query), plan_status_filter)


def dense_ranking(query: str, k: int = CANDIDATES,
                  plan_status_filter: str = "all") -> list[tuple[str, float]]:
    """Dense leg — ineligible chunks are masked out BEFORE the top-k cutoff."""
    return _dense_ranking_full(query, plan_status_filter)[:k]


def dense_dim() -> int:
    """Vector dimension of the dense snapshot — the warm-up builds its probe
    vector from this instead of hardcoding 1024, so a future index of another
    width cannot silently produce a shape-mismatched warm pass."""
    return int(_dense_index().vecs.shape[1])


def warm_local(query: str, query_vector) -> str | None:
    """Load every retrieval cache a first query would otherwise pay for, without
    embedding anything remotely. Returns the top-ranked chunk_id (or None).

    Covers: chunk metadata, BOTH plan-status masks (the "current" mask is built
    lazily and a plain "all" warm would leave it cold), the Kiwi analyser and the
    FTS statistics via one real BM25 query, and the dense matrix path.
    """
    _chunk_metadata()
    for plan_status_filter in PLAN_STATUS_FILTERS:
        _search_mask(plan_status_filter)
        # both BM25 variants: the plan-status branch is a different SQL statement
        bm25_ranking(query, CANDIDATES, plan_status_filter)
    hits = rank_precomputed_embedding(query_vector, "all")
    return hits[0][0] if hits else None


def _select(sparse_ids: list[str], dense_hits: list[tuple[str, float]],
            k: int, family_cap: int) -> tuple[list[tuple[str, float]], dict, dict, dict]:
    """Fuse by RRF on RAW ranks and apply the family cap.
    Returns (selected [(chunk_id, rrf)], sparse_rank, dense_rank, dense_score)."""
    sparse_rank = {cid: i for i, cid in enumerate(sparse_ids)}
    dense_rank = {cid: i for i, (cid, _) in enumerate(dense_hits)}
    dense_score = dict(dense_hits)

    fused: dict[str, float] = {}
    for cid, r in sparse_rank.items():
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + r + 1)
    for cid, r in dense_rank.items():
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + r + 1)
    # tie-break on chunk_id so equal RRF scores order deterministically
    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))

    meta = _chunk_metadata()
    selected, per_family = [], {}
    for cid, rrf in ranked:
        m = meta.get(cid)
        if m is None or not m[2]:
            continue  # defense in depth — legs already filtered on eligibility
        fam = m[0]
        if per_family.get(fam, 0) >= family_cap:
            continue
        per_family[fam] = per_family.get(fam, 0) + 1
        selected.append((cid, rrf))
        if len(selected) >= k:
            break
    return selected, sparse_rank, dense_rank, dense_score


def hybrid_search(query: str, k: int = 8, family_cap: int = 2,
                  mode: str = "hybrid", plan_status_filter: str = "all") -> list[dict]:
    """Returns citable candidates with internal scores (server strips scores).

    Candidate windows start at CANDIDATES and widen only when the family cap
    leaves fewer than k results; ranks stay raw across rounds. Returning fewer
    than k is correct once every active leg is exhausted and the corpus simply
    has no k results that satisfy the cap.

    plan_status_filter: "all" (default — plans, non-plans and superseded alike)
    or "current" (only documents bound to a curated plan verified as current).
    Never chosen automatically by the server — the caller opts in.
    """
    if plan_status_filter not in PLAN_STATUS_FILTERS:
        raise ValueError(f"plan_status_filter must be one of {PLAN_STATUS_FILTERS}")
    verify_snapshot_consistency()
    use_sparse = mode in ("hybrid", "bm25")
    use_dense = mode in ("hybrid", "dense")
    dense_all = _dense_ranking_full(query, plan_status_filter) if use_dense else []

    depth = CANDIDATES
    selected: list[tuple[str, float]] = []
    sparse_rank = dense_rank = dense_score = {}
    while True:
        sparse_ids = bm25_ranking(query, depth, plan_status_filter) if use_sparse else []
        dense_hits = dense_all[:depth] if use_dense else []
        selected, sparse_rank, dense_rank, dense_score = _select(
            sparse_ids, dense_hits, k, family_cap)
        if len(selected) >= k or depth >= MAX_CANDIDATES:
            break
        sparse_done = not use_sparse or len(sparse_ids) < depth
        dense_done = not use_dense or len(dense_hits) >= len(dense_all)
        if sparse_done and dense_done:
            break  # both legs exhausted — a short result set is the honest answer
        depth = min(depth * EXPANSION_FACTOR, MAX_CANDIDATES)

    if not selected:
        return []
    con = _con()
    placeholders = ",".join("?" * len(selected))
    rows = {r["chunk_id"]: r for r in con.execute(
        f"SELECT * FROM chunks WHERE chunk_id IN ({placeholders})",
        [cid for cid, _ in selected])}
    con.close()

    out = []
    for cid, rrf in selected:  # preserve fused order
        row = rows.get(cid)
        if row is None:
            continue
        rec = dict(row)
        rec["locator"] = json.loads(rec["locator"])
        rec.update({
            "rrf_score": round(rrf, 5),
            "sparse_rank": sparse_rank.get(cid),
            "dense_rank": dense_rank.get(cid),
            "dense_score": round(dense_score.get(cid, 0.0), 4) if cid in dense_score else None,
        })
        out.append(rec)
    return out


def _gap_candidates() -> list[dict]:
    """Documents that could ever produce a gap notice, with their title tokens
    pre-computed. Parsed once per process: this used to re-read and re-tokenize
    the whole documents.jsonl on every single search."""
    global _gap_candidate_cache
    if _gap_candidate_cache is None:
        cands = []
        for line in (VAULT / "meta" / "documents.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if d["ext"] != "pdf":
                continue
            n = d.get("n_pages") or 0
            counts = d.get("page_status_counts", {})
            bad = sum(counts.get(s, 0) for s in GAP_PAGE_STATUS)
            if not n or bad / n <= 0.5:
                continue
            cands.append({
                "doc_id": d["doc_id"],
                "title": d["title"],
                "gap_type": "image_only"
                if counts.get("image_only", 0) >= counts.get("suspect_encoding", 0)
                else "suspect_encoding",
                "reason": f"{n}페이지 중 {bad}페이지 미색인 — 이 문서에 대한 근거 부재 판단 불가",
                "title_tokens": {t for t in tokenize_ko.search_tokens(d["title"]).split()
                                 if len(t) >= 2 and not t.isdigit()},
            })
        _gap_candidate_cache = cands
    return _gap_candidate_cache


def extraction_gaps(query: str) -> list[dict]:
    """Structured gap notices: documents whose title matches the query but whose
    pages are largely non-indexable (canonical GAP_PAGE_STATUS). Heuristic —
    an empty list does NOT mean the corpus is complete."""
    q = tokenize_ko.search_tokens(query)
    if not q:
        return []
    qset = {t for t in q.split() if len(t) >= 2 and not t.isdigit()}
    return [{"doc_id": c["doc_id"], "title": c["title"],
             "gap_type": c["gap_type"], "reason": c["reason"]}
            for c in _gap_candidates() if len(qset & c["title_tokens"]) >= 3]
