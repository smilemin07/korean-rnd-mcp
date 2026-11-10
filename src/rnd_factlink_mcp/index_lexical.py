"""Lexical index build (Phase 2A): SQLite chunks table + two FTS5 indexes.

- fts_kiwi: Kiwi pre-tokenized content morphemes (title / institution / body
  columns, BM25 weights title 3.0, inst 2.0, body 1.0)
- fts_tri: trigram tokenizer baseline on normalized raw text (no morphology)

The DB is rebuilt atomically: written to lexical.db.tmp then renamed over
vault/index/lexical.db (previous snapshot preserved as lexical.db.prev).
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import chunker, tokenize_ko
from .ingest import PIPELINE_VERSION, VAULT, _load_jsonl

INDEX_VERSION = "lex1.1"  # lex1.1: plan_id / plan_status columns for curated-plan filtering
DB_PATH = VAULT / "index" / "lexical.db"

SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chunks (
  rowid INTEGER PRIMARY KEY,
  chunk_id TEXT UNIQUE NOT NULL,
  doc_id TEXT NOT NULL,
  family_id TEXT,
  variant_type TEXT,
  authority_tier INTEGER,
  evidence_eligible INTEGER,
  title TEXT,
  institution TEXT,
  filename_date TEXT,
  source_folder TEXT,
  plan_id TEXT,
  plan_status TEXT,
  locator TEXT NOT NULL,
  page_start INTEGER,
  page_end INTEGER,
  n_tokens INTEGER NOT NULL,
  text_raw TEXT NOT NULL,
  text_norm TEXT NOT NULL
);
CREATE INDEX idx_chunks_doc ON chunks(doc_id);
CREATE INDEX idx_chunks_family ON chunks(family_id);
CREATE VIRTUAL TABLE fts_kiwi USING fts5(title, inst, body, content='');
CREATE VIRTUAL TABLE fts_tri USING fts5(body, content='', tokenize='trigram');
"""


def build(doc_allowlist: set[str] | None = None, db_path: Path | None = None) -> int:
    """Build the lexical index. Default: full dev index at DB_PATH with .prev
    swap. With doc_allowlist (public-bundle mode): only allowed documents are
    chunked/indexed — FTS statistics come from the public corpus alone — and
    source_folder (private root folder names) is redacted from the DB."""
    public = doc_allowlist is not None
    target = db_path or DB_PATH
    t0 = time.perf_counter()
    docs = _load_jsonl(VAULT / "meta" / "documents.jsonl")
    if not docs:
        print("FATAL: documents.jsonl missing — run ingest first")
        return 1
    if public:
        docs = [d for d in docs if d["doc_id"] in doc_allowlist]
        missing = doc_allowlist - {d["doc_id"] for d in docs}
        if missing:
            print(f"FATAL: allowlist doc_ids not in documents.jsonl: {sorted(missing)}")
            return 1

    tmp = target.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA_SQL)

    n_chunks = 0
    docs_without_chunks = []
    for doc in docs:
        docmeta = json.loads((VAULT / "derived" / "docmeta" / f"{doc['doc_id']}.json")
                             .read_text(encoding="utf-8"))
        units = _load_jsonl(VAULT / "derived" / "text" / f"{doc['doc_id']}.jsonl")
        recs = chunker.chunk_document(doc, docmeta["pages"], units)
        if not recs:
            docs_without_chunks.append(doc["doc_id"])
            continue
        title_toks = tokenize_ko.search_tokens(doc["title"])
        inst_toks = tokenize_ko.search_tokens(
            f"{doc.get('institution_raw') or ''} {doc.get('institution_normalized') or ''}".strip())
        for r in recs:
            n_chunks += 1
            con.execute(
                "INSERT INTO chunks (chunk_id, doc_id, family_id, variant_type, authority_tier,"
                " evidence_eligible, title, institution, filename_date, source_folder,"
                " plan_id, plan_status, locator,"
                " page_start, page_end, n_tokens, text_raw, text_norm)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["chunk_id"], doc["doc_id"], doc.get("family_id"), doc["variant_type"],
                 doc["authority_tier"], int(doc["evidence_eligible"]), doc["title"],
                 doc.get("institution_normalized"), doc.get("filename_date"),
                 None if public else doc["source_folder"],
                 doc.get("plan_id"), doc.get("plan_status"),
                 json.dumps(r["locator"], ensure_ascii=False),
                 r["page_start"], r["page_end"], r["n_tokens"], r["text_raw"], r["text_norm"]))
            rowid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            con.execute("INSERT INTO fts_kiwi (rowid, title, inst, body) VALUES (?,?,?,?)",
                        (rowid, title_toks, inst_toks, tokenize_ko.search_tokens(r["text_norm"])))
            con.execute("INSERT INTO fts_tri (rowid, body) VALUES (?,?)", (rowid, r["text_norm"]))

    import importlib.metadata as im
    for k, v in {
        "index_version": INDEX_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "kiwipiepy_version": im.version("kiwipiepy"),
        "chunk_params": json.dumps({"target": chunker.TARGET_TOKENS, "hard_max": chunker.HARD_MAX_TOKENS,
                                    "overlap": chunker.OVERLAP_TOKENS, "max_pages": chunker.MAX_PAGES_PER_CHUNK}),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_docs": str(len(docs)), "n_chunks": str(n_chunks),
        "docs_without_chunks": json.dumps(docs_without_chunks),
    }.items():
        con.execute("INSERT INTO meta VALUES (?,?)", (k, v))
    con.commit()
    con.close()

    # atomic swap; the dev index keeps a .prev snapshot, a public bundle doesn't
    if not public and target.is_file():
        target.replace(target.with_suffix(".db.prev"))
    tmp.replace(target)

    # gate checks
    con = sqlite3.connect(target)
    n_c = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    n_k = con.execute("SELECT count(*) FROM fts_kiwi").fetchone()[0]
    n_t = con.execute("SELECT count(*) FROM fts_tri").fetchone()[0]
    over = con.execute("SELECT count(*) FROM chunks WHERE n_tokens > ?",
                       (chunker.HARD_MAX_TOKENS + chunker.OVERLAP_TOKENS,)).fetchone()[0]
    con.close()
    # docs without chunks are acceptable ONLY when every page is a recorded
    # extraction gap (image_only/empty/suspect/error) — never a silent loss
    unexplained = []
    for doc_id in docs_without_chunks:
        pages = json.loads((VAULT / "derived" / "docmeta" / f"{doc_id}.json")
                           .read_text(encoding="utf-8"))["pages"]
        if any(p["page_status"] in chunker.INDEXABLE_PAGE_STATUS for p in pages):
            unexplained.append(doc_id)
    ok = n_c == n_k == n_t == n_chunks and over == 0 and not unexplained
    print(f"chunks={n_c} fts_kiwi={n_k} fts_tri={n_t} over_budget={over} "
          f"docs_without_chunks={docs_without_chunks} (unexplained={unexplained}) "
          f"seconds={time.perf_counter()-t0:.1f}")
    print(f"GATE 2A build {'PASS' if ok else 'FAIL'}: {target}")
    return 0 if ok else 1


def search(query: str, mode: str = "kiwi", k: int = 20, family_cap: int = 2) -> list[dict]:
    """BM25 search. family_cap limits results per family (변형 문서 독점 방지)."""
    if mode == "kiwi":
        match = tokenize_ko.fts_match_query(query)   # quoted tokens (S1 fix)
        if not match:
            return []
        sql = ("SELECT c.*, bm25(fts_kiwi, 3.0, 2.0, 1.0) AS score FROM fts_kiwi"
               " JOIN chunks c ON c.rowid = fts_kiwi.rowid"
               " WHERE fts_kiwi MATCH ? ORDER BY score LIMIT ?")
    else:
        # trigram: per-word phrase terms OR-ed; words under 3 chars cannot match
        # (documented FTS5 trigram limitation — e.g. "예타" is unsearchable)
        words = [w for w in query.replace('"', " ").split() if len(w) >= 3]
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in words)
        sql = ("SELECT c.*, bm25(fts_tri) AS score FROM fts_tri"
               " JOIN chunks c ON c.rowid = fts_tri.rowid"
               " WHERE fts_tri MATCH ? ORDER BY score LIMIT ?")
    # Open only once there is something to run; close on every path; never let a
    # raw sqlite message (it can quote query tokens) escape — same boundary as
    # search_hybrid.bm25_ranking.
    from .search_hybrid import LexicalUnavailable
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(sql, (match, k * 5)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise LexicalUnavailable(type(e).__name__) from None

    out, per_family = [], {}
    for r in rows:
        fam = r["family_id"] or r["doc_id"]
        if per_family.get(fam, 0) >= family_cap:
            continue
        per_family[fam] = per_family.get(fam, 0) + 1
        out.append(dict(r))
        if len(out) >= k:
            break
    return out
