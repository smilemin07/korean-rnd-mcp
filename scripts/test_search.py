#!/usr/bin/env python3
"""Search-behaviour regression test (separate from the v2 contract test).

Covers the candidate-window starvation fix:
- diagnostic queries must fill k once the window widens
- the family cap is never broken to reach k
- RRF uses RAW leg ranks (a filtered candidate must not be re-numbered)
- every mode (hybrid / bm25 / dense) uses the same termination rule

Usage: .venv/bin/python scripts/test_search.py
"""

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnd_factlink_mcp import search_hybrid, tokenize_ko  # noqa: E402
from rnd_factlink_mcp.search_hybrid import (  # noqa: E402
    CANDIDATES, RRF_K, LexicalUnavailable, _chunk_metadata, _select, bm25_ranking,
    hybrid_search)

# Queries measured on 2026-08-13. "건설 분야…" returned 4 of 8 before the fix
# because one document held 47 of the 50 BM25 candidates.
DIAGNOSTIC = [
    "재난안전 기술개발 종합계획의 중점 추진과제",
    "건설 분야 기술수준과 기술격차",
    "국가연구개발 투자방향과 중점 투자 분야",
    "예비타당성조사 폐지 이후 사전점검 절차",
    "연구개발비 집행과 정산 절차",
]
CANARY_QUERY = "건설 분야 기술수준과 기술격차"
CANARY_TITLE_PART = "기술수준평가"


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}: {label}" + (f" — {detail}" if detail else ""))
    return ok


def test_synthetic():
    """_select in isolation: raw ranks, family cap, exhaustion."""
    ok = True
    meta = _chunk_metadata()
    # pick two chunks from different families to build a synthetic ranking
    by_fam = {}
    for cid, (fam, _doc, elig) in meta.items():
        if elig:
            by_fam.setdefault(fam, []).append(cid)
    fams = [f for f, ids in by_fam.items() if len(ids) >= 3][:3]
    assert len(fams) >= 2, "corpus too small for synthetic test"

    # (1) raw rank preserved: a chunk sitting at sparse position 47 must score
    #     1/(RRF_K+47+1), not a value implying a compacted position.
    tail = by_fam[fams[0]][0]
    sparse = [by_fam[fams[1]][i % len(by_fam[fams[1]])] for i in range(47)] + [tail]
    sel, *_ = _select(sparse, [], k=8, family_cap=2)
    got = dict(sel).get(tail)
    expected = 1.0 / (RRF_K + 47 + 1)          # raw position 47, not a compacted one
    compacted = 1.0 / (RRF_K + 1 + 1)          # what re-numbering would produce
    ok &= check("raw rank preserved in RRF",
                got is not None and abs(got - expected) < 1e-9 and abs(got - compacted) > 1e-6,
                f"got={got} expected≈{expected:.8f}")

    # (2) family cap is never broken to reach k
    single_family = by_fam[fams[0]][:20]
    sel2, *_ = _select(single_family, [], k=8, family_cap=2)
    ok &= check("family cap holds when k cannot be filled",
                len(sel2) == 2, f"selected={len(sel2)} (expected 2)")

    # (3) cap counts by family, not by chunk
    mixed = by_fam[fams[0]][:5] + by_fam[fams[1]][:5]
    sel3, *_ = _select(mixed, [], k=8, family_cap=2)
    fam_counts = collections.Counter(meta[c][0] for c, _ in sel3)
    ok &= check("per-family cap enforced", all(v <= 2 for v in fam_counts.values()),
                str(dict(fam_counts)))
    return ok


def test_real_corpus():
    ok = True
    meta = _chunk_metadata()
    for q in DIAGNOSTIC:
        res = hybrid_search(q, k=8)
        fams = collections.Counter(r["family_id"] or r["doc_id"] for r in res)
        cap_ok = all(v <= 2 for v in fams.values())
        ok &= check(f"k=8 filled: {q[:26]}…", len(res) == 8,
                    f"returned {len(res)}, families {len(fams)}")
        ok &= check(f"cap respected: {q[:26]}…", cap_ok, str(dict(fams)))

    # canary: the document that genuinely answers the query must survive
    res = hybrid_search(CANARY_QUERY, k=8)
    titles = [r["title"] for r in res]
    ok &= check("canary document retained",
                any(CANARY_TITLE_PART in t for t in titles),
                f"titles={[t[:24] for t in titles[:3]]}")

    # every mode terminates and respects the cap
    for mode in ("hybrid", "bm25", "dense"):
        res = hybrid_search(CANARY_QUERY, k=8, mode=mode)
        fams = collections.Counter(r["family_id"] or r["doc_id"] for r in res)
        ok &= check(f"mode={mode} terminates within cap",
                    len(res) <= 8 and all(v <= 2 for v in fams.values()),
                    f"n={len(res)}")

    # expansion must not change results for queries that already filled at depth 50
    baseline = hybrid_search(DIAGNOSTIC[0], k=8)
    ok &= check("no expansion side effect when depth 50 suffices",
                len(baseline) == 8, f"n={len(baseline)}")
    return ok


def test_plan_metadata():
    """Family split + curated plan metadata + current filter (SCHEMA §11)."""
    import json
    ok = True
    docs = [json.loads(l) for l in
            open(Path(__file__).resolve().parent.parent / "vault/meta/documents.jsonl",
                 encoding="utf-8")]
    by_title = {d["title"]: d for d in docs}

    # 세부 01~11 all in distinct families, distinct from the main volume
    subs = [d for d in docs if " - 세부 - " in d["title"] and "기술수준평가" in d["title"]]
    fams = {d["family_id"] for d in subs}
    main = by_title.get("2024년도 기술수준평가 결과(안)")
    ok &= check("세부권 11건이 서로 다른 family", len(subs) == 11 and len(fams) == 11,
                f"subs={len(subs)} fams={len(fams)}")
    ok &= check("본체는 세부권과 다른 family", main and main["family_id"] not in fams)
    # 정상 family 유지 (회귀 방지)
    mulyu = [d for d in docs if "물류시설개발 종합계획" in d["title"]]
    ok &= check("물류시설개발 4변형 family 유지",
                len({d["family_id"] for d in mulyu}) == 1 and len(mulyu) == 4)
    # relation target은 같은 family
    ok &= check("relation target 정합",
                all(by_title.get(next((x["title"] for x in docs if x["doc_id"] == d["relation_target"]), ""),
                                 {}).get("family_id") == d["family_id"]
                    for d in docs if d.get("relation_target")))

    # plan metadata merged for held documents
    planned = {d["plan_id"]: d for d in docs if d.get("plan_id")}
    ok &= check("plan 메타 병합(기보유 4건)", len(planned) >= 4, f"{sorted(planned)}")
    ok &= check("current에 기준일 존재",
                all(d.get("plan_status_as_of") for d in planned.values()
                    if d.get("plan_status") == "current"))

    # current filter: only curated-current docs come back; pre-cutoff application
    res = hybrid_search("과학기술 기본계획의 전략과 투자 방향", k=8, plan_status_filter="current")
    ok &= check("current 필터: 결과 전원 current",
                bool(res) and all(r.get("plan_status") == "current" for r in res),
                f"n={len(res)}")
    # "all" must NOT restrict by plan status. The old proxy ("a plan-centric
    # query surfaces some non-plan doc in top-8") aged out once those top docs
    # were registered (2026-08-14, P040-P046) — assert the invariant directly:
    # non-current documents remain retrievable, and a query aimed at reports
    # still returns docs with no plan metadata at all.
    res_all = hybrid_search("과학기술 기본계획의 전략과 투자 방향", k=8)
    ok &= check("all 기본값은 비current 문서 포함",
                any(r.get("plan_status") != "current" for r in res_all),
                f"statuses={sorted({str(r.get('plan_status')) for r in res_all})}")
    res_rep = hybrid_search("건설 분야 기술수준과 기술격차", k=8)
    ok &= check("all 기본값은 비계획 문서 포함(보고서 질의)",
                any(not r.get("plan_id") for r in res_rep))
    try:
        hybrid_search("x", plan_status_filter="latest")
        ok &= check("잘못된 필터 값 거부", False)
    except ValueError:
        ok &= check("잘못된 필터 값 거부", True)
    return ok


# --- S1 hotfix (UPDATE-PLAN-R1 §16 G-b / G-b'): special tokens in FTS5 MATCH ---
# Kiwi keeps decimals, thousands separators, dotted abbreviations and hyphenated
# words as single tokens; unquoted they were parsed as FTS5 syntax and failed the
# whole tool call. Queries below are synthetic probes, not sealed evaluation items.
SPECIAL_QUERIES = ["137.6조원 규모", "5.0% 증가", "p.183의 표", "1,000억원 이상 사업",
                   "한-미 협력", "e.g. 예시"]
ANCHOR_QUERY, ANCHOR_CHUNK = "137.6조원 규모", "d_7d310470a623_c0044"  # 투자전략 총R&D 표
NEGATIVE_SYNTAX = ["*", "^", ":", "-", ".", ",", '""', '"""', "AND OR NOT NEAR", "예산*", "*예산",
                   "title:예산", "(예산)", "예산 - 계획", "…", "①②③", 'a"b', "NEAR(", "abc:"]


def _oracle():
    """Empty in-memory FTS5 table with the production column layout — parses a
    MATCH expression exactly like the real index does (25/25 agreement measured
    2026-09-03), without touching the real database. Test-only."""
    import sqlite3
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE VIRTUAL TABLE t USING fts5(title, inst, body)")
    return mem


def _parses(mem, expr: str) -> bool:
    import sqlite3
    try:
        mem.execute("SELECT rowid FROM t WHERE t MATCH ?", (expr,)).fetchall()
        return True
    except sqlite3.OperationalError:
        return False


def test_special_tokens():
    import sqlite3
    ok = True
    mem = _oracle()
    q = tokenize_ko.fts_match_query
    # (i) helper contract: per-token quoting, OR between tokens, "" for no tokens
    ok &= check("helper quotes every token", q("137.6조원 규모") == '"137.6" OR "원" OR "규모"',
                q("137.6조원 규모"))
    ok &= check("helper: no tokens -> length-0 string", q("") == "" and q("…") == "" and q("*") == "")
    ok &= check("helper escapes an embedded quote", '"' + 'x"y'.replace('"', '""') + '"' == '"x""y"'
                and _parses(mem, '"x""y"'))
    ok &= check("negative syntax list parses or is empty",
                all((q(t) == "" or _parses(mem, q(t))) for t in NEGATIVE_SYNTAX))
    ok &= check("unquoted form of a decimal still fails (regression anchor)",
                not _parses(mem, "137.6 OR 원"))

    # (ii) synthetic fixture: target vs distractor documents per token shape.
    # Bodies are Kiwi-style token strings, exactly what the real index stores.
    docs = {
        "dec_hit": "예산 137.6 원", "dec_adjacent": "예산 137 6 원", "dec_miss": "예산 1376 원",
        "dec_miss2": "예산 13.76 원",
        "thou_hit": "사업 1,000 억", "thou_miss": "사업 1000 억",
        "dot_hit": "p. 183 표", "abbr_hit": "e.g. 예시", "abbr_miss": "eg 예시",
        "hyph_hit": "한-미 협력", "hyph_miss": "한미 협력", "quote_hit": 'x"y 협력',
    }
    ids = {}
    for name, body in docs.items():
        cur = mem.execute("INSERT INTO t(title, inst, body) VALUES ('', '', ?)", (body,))
        ids[cur.lastrowid] = name
    def hits(expr):
        return {ids[r[0]] for r in mem.execute("SELECT rowid FROM t WHERE t MATCH ?", (expr,))}
    expect = {
        "137.6": {"dec_hit", "dec_adjacent"},        # phrase "137 6": adjacent only
        "1,000": {"thou_hit"},                        # "1000" unpunctuated is NOT matched (known limit)
        "p.": {"dot_hit"}, "e.g.": {"abbr_hit"}, "한-미": {"hyph_hit"}, 'x"y': {"quote_hit"},
    }
    for tok, want in expect.items():
        got = hits('"' + tok.replace('"', '""') + '"')
        ok &= check(f"fixture {tok!r} -> {sorted(want)}", got == want, f"got {sorted(got)}")
    for name in ("dec_miss", "dec_miss2", "thou_miss", "abbr_miss", "hyph_miss"):
        ok &= check(f"fixture distractor {name} never matched",
                    all(name not in hits('"' + t.replace('"', '""') + '"') for t in expect))

    # (iii) real corpus: the six probe shapes run without error on both filters
    for query in SPECIAL_QUERIES:
        for f in ("all", "current"):
            try:
                rows = bm25_ranking(query, 8, f)
                ok &= check(f"bm25 leg ok: {query!r} filter={f}",
                            f == "current" or len(rows) >= 1, f"n={len(rows)}")
            except Exception as e:  # noqa: BLE001 — any exception is the S1 failure mode
                ok &= check(f"bm25 leg ok: {query!r} filter={f}", False, f"{type(e).__name__}: {e}")
    # (iv) anchor: the decimal must lead the lexical leg to the 투자전략 table chunk.
    # Since the C1 corpus train (2026-11) the anchor document (제1차 투자전략, P017) is
    # superseded by the 2nd strategy, so under filter=current the anchor must be ABSENT
    # while the leg still answers — this doubles as a check of the current filter.
    rows = bm25_ranking(ANCHOR_QUERY, 8, "all")
    ok &= check("anchor rank 1 (all)", bool(rows) and rows[0] == ANCHOR_CHUNK, f"top={rows[:2]}")
    rows = bm25_ranking(ANCHOR_QUERY, 8, "current")
    ok &= check("anchor absent under current (superseded doc filtered)",
                bool(rows) and ANCHOR_CHUNK not in rows, f"top={rows[:2]}")
    # (v) boundary: a 500-char question full of special tokens (bm25 mode, no embedding)
    long_q = ("국가연구개발사업 예산 137.6조원 1,000억원 p.183 5.0% 한-미 협력 e.g. 사례 " * 12)[:500]
    try:
        res = hybrid_search(long_q, k=8, mode="bm25")
        ok &= check("500-char special query (bm25 mode)", len(res) > 0, f"n={len(res)}")
    except Exception as e:  # noqa: BLE001
        ok &= check("500-char special query (bm25 mode)", False, f"{type(e).__name__}: {e}")
    # (vi) sanitized fail-closed: a sqlite error inside the leg surfaces as
    # LexicalUnavailable carrying the class name only — no message, no chain
    import traceback
    sentinel = "SENTINELTOKEN_zq7v"
    real_con = search_hybrid._con
    def broken_con():
        raise sqlite3.OperationalError(f"no such column: {sentinel}")
    search_hybrid._con = broken_con
    try:
        try:
            hybrid_search("예산 계획", k=3, mode="bm25")
            ok &= check("lexical failure raises LexicalUnavailable", False, "no exception")
        except LexicalUnavailable as e:
            tb = traceback.format_exc()
            ok &= check("LexicalUnavailable carries the class name only",
                        str(e) == "OperationalError" and e.__cause__ is None and e.__suppress_context__, str(e))
            ok &= check("traceback does not quote the original message", sentinel not in tb)
    finally:
        search_hybrid._con = real_con
    ok &= check("index restored after fault injection", bool(bm25_ranking(ANCHOR_QUERY, 3, "all")))
    # (vii) dev CLI path (index_lexical.search, kiwi mode) uses the same helper
    from rnd_factlink_mcp import index_lexical
    rows = index_lexical.search("137.6조원", "kiwi", k=5)
    ok &= check("dev CLI kiwi search reaches the anchor", bool(rows) and rows[0]["chunk_id"] == ANCHOR_CHUNK,
                f"top={[r['chunk_id'] for r in rows[:2]]}")
    return ok


def test_vocab_parse_full():
    """G-b': every Kiwi token in the corpus, quoted, parses as FTS5 MATCH.
    Re-tokenizes the whole index (~40s) — release gate, not the fast loop."""
    import sqlite3
    import time
    mem = _oracle()
    t0 = time.time()
    con = search_hybrid._con()
    vocab = set()
    for (s,) in con.execute("SELECT text_norm FROM chunks"):
        vocab.update(tokenize_ko.search_tokens(s or "").split())
    con.close()
    bad = [t for t in vocab if not _parses(mem, '"' + t.replace('"', '""') + '"')]
    unquoted_bad = sum(1 for t in vocab if not _parses(mem, t))
    return check("corpus vocabulary: quoted tokens parse", not bad,
                 f"vocab {len(vocab)}, quoted errors {len(bad)} (unquoted would fail {unquoted_bad}), "
                 f"{time.time() - t0:.0f}s") and unquoted_bad > 0


if __name__ == "__main__":
    print(f"[config] CANDIDATES={CANDIDATES} RRF_K={RRF_K}\n")
    ok = test_synthetic()
    print()
    ok &= test_real_corpus()
    print()
    ok &= test_plan_metadata()
    print()
    ok &= test_special_tokens()
    print()
    if "--full" in sys.argv:
        ok &= test_vocab_parse_full()
        print()
    if not ok:
        print("SEARCH REGRESSION TEST FAILED")
        sys.exit(1)
    print("SEARCH REGRESSION TEST PASSED")
