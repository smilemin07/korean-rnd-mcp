#!/usr/bin/env python3
"""Public-bundle smoke test — run against a build_public_bundle.py output.

Usage: .venv/bin/python scripts/test_public_bundle.py dist/bundle-<ts>

Boots the BUNDLED code (PYTHONPATH=<bundle>/src) in HTTP mode — the same tree
the container runs — and verifies:
1. v2 contract envelope + passage roundtrip on the public index
2. excluded documents are dark: metadata lookups error, search never returns
   their doc_ids, chunk lookups refuse
3. top-5 overlap between the dev index and the public index for sample
   queries (reported, not asserted — corpus statistics legitimately differ)
4. R1b: served-docs-without-URL set equals the approved set (from the bundle's own
   files — exact), manifest URL status (null + "unavailable" vs registered)
   follows that approved set, the readiness diagnostics file carries
   the registry digests, and the real HTTP wire size of a tool result is recorded
5. S1 hotfix (UPDATE-PLAN-R1 §16 G-d): special-token questions return
   candidates over HTTP (the decimal one reaches the 투자전략 table chunk), and
   a separate process running ONLY the bundled code checks the BM25 anchor and
   that every token of the public corpus, quoted, parses as FTS5 MATCH

Embed backend for the subprocess defaults to local (Mac); set
RND_FACTLINK_EMBED_BACKEND=workersai with credentials for a prod-like run.
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

PORT = 8972
URL = f"http://127.0.0.1:{PORT}/mcp"

CANARY_Q = "학생인건비 잔액은 어떻게 반납하는가?"
OVERLAP_QUERIES = [CANARY_Q, "정부 연구개발 투자방향 및 기준의 중점 투자 분야",
                   "제7차 건설기술진흥 기본계획의 추진 전략"]


async def run_checks(bundle: Path, excluded: set[str], excluded_titles: list[str]) -> None:
    from fastmcp import Client
    async with Client(URL) as c:
        # 1. contract envelope + roundtrip
        resp = (await c.call_tool("search_rnd_facts", {"question": CANARY_Q})).data
        assert resp["response_schema_version"] == "v2"
        assert resp["retrieval_status"] == "candidates_returned"
        assert resp["support_status"] == "not_assessed"
        for r in resp["results"]:
            assert r["citation"]["citation_allowed"] is True
            assert r["citation"]["authority_tier"] in (1, 2)
            assert r["citation"]["doc_id"] not in excluded
        chunk = resp["results"][0]
        p = (await c.call_tool("get_passages", {"chunk_ids": [chunk["chunk_id"]]})).data
        assert p["passages"][0].get("text_complete") is True
        meta = (await c.call_tool("get_document_metadata",
                                  {"doc_id": chunk["citation"]["doc_id"]})).data
        assert meta.get("servable_scope") == "public_alpha", meta.get("servable_scope")
        assert meta.get("source_folder") is None, "internal field leaked"
        print("[public] envelope + roundtrip + public metadata OK")

        # 2. excluded docs are dark. The probe set is derived at run time from the
        # dev documents the bundle left out (no titles are kept in code). When the
        # dev corpus has nothing left out, an id that exists nowhere must still be
        # refused, so the refusal paths are exercised either way.
        if not excluded:
            print("[public] dev and public document sets are identical — no excluded docs to probe")
        probe_ids = sorted(excluded)[:6] or ["d_000000000000"]
        for did in probe_ids:
            m = (await c.call_tool("get_document_metadata", {"doc_id": did})).data
            assert "error" in m, f"excluded doc visible: {did}"
        for q in excluded_titles[:6]:
            resp = (await c.call_tool("search_rnd_facts", {"question": q})).data
            hit = [r["citation"]["doc_id"] for r in resp.get("results", [])
                   if r["citation"]["doc_id"] in excluded]
            assert not hit, f"excluded doc in results for excluded-title probe: {hit}"
        # chunk of an excluded doc (dev chunk id pattern) must be refused
        probe = (await c.call_tool("get_passages",
                                   {"chunk_ids": [f"{probe_ids[0]}_c0000"]})).data
        assert "error" in probe["passages"][0]
        print(f"[public] {len(excluded)} excluded docs dark in metadata/search/passages OK")

        # 4a. manifest URL status follows the approved null set (R1b C2). Since
        # 2026-11-10 the approved set is empty (documents without a source URL are
        # not served); the unavailable branch is covered by test_contract.py #6.
        approved_nulls = set(json.loads((bundle / "curation" / "expected_null_source_urls.json")
                                        .read_text(encoding="utf-8"))["doc_ids"])
        resp = (await c.call_tool("search_rnd_facts",
                                  {"question": "정부 연구개발 투자방향 및 기준의 중점 투자 분야"})).data
        assert "candidate_source_manifest" in resp and resp["answer_requirements"]["mark_missing_source_url"]
        assert resp["candidate_source_manifest"], "empty manifest"
        for m in resp["candidate_source_manifest"]:
            null = m["doc_id"] in approved_nulls
            assert (m["source_url"] is None) == null, m
            assert m["source_url_status"] == ("unavailable" if null else "registered"), m
        print(f"[public-r1b] manifest URL status matches the approved null set "
              f"({len(approved_nulls)} docs) OK")

        # 5. S1 hotfix — data-level checks (an error dict is a normal result)
        for q, anchor in [("137.6조원 규모", "d_7d310470a623_c0044"), ("1,000억원 이상 사업", None)]:
            resp = (await c.call_tool("search_rnd_facts", {"question": q})).data
            assert "error" not in resp, f"{q!r}: {resp.get('error')}"
            assert resp["retrieval_status"] == "candidates_returned" and resp["results"], q
            if anchor:
                ids = [r["chunk_id"] for r in resp["results"]]
                assert anchor in ids, f"anchor {anchor} not in {ids}"
        print("[public-s1] special-token questions return candidates; decimal reaches the anchor OK")

        # 3. dev↔public top-5 overlap (report only)
        os.environ["RND_FACTLINK_EMBED_BACKEND"] = "local"
        from rnd_factlink_mcp.search_hybrid import hybrid_search
        for q in OVERLAP_QUERIES:
            dev_top = [r["chunk_id"] for r in hybrid_search(q, k=5)]
            resp = (await c.call_tool("search_rnd_facts", {"question": q})).data
            pub_top = [r["chunk_id"] for r in resp["results"]]
            inter = len(set(dev_top) & set(pub_top))
            print(f"[overlap] {inter}/5  {q[:38]}")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: test_public_bundle.py dist/bundle-<ts>")
        return 2
    bundle = Path(sys.argv[1]).resolve()
    manifest = json.loads((bundle / "release_manifest.json").read_text(encoding="utf-8"))
    print(f"bundle {manifest['bundle']} | docs {manifest['n_docs_served']} "
          f"| chunks {manifest['n_chunks']} | fp {manifest['snapshot_fingerprint']} "
          f"| release {manifest.get('release_id')}")
    # C8: the release id must be present and must match what the bundled code
    # recomputes from the file digests — the cutover script compares against it
    from rnd_factlink_mcp.readiness import compute_release_id
    assert manifest.get("release_id") == compute_release_id(manifest), \
        "release_id missing or inconsistent with the manifest file digests"

    # excluded set = all dev docs minus bundled docs
    dev_docs = [json.loads(l) for l in
                open(ROOT / "vault" / "meta" / "documents.jsonl", encoding="utf-8")]
    dev_ids = {d["doc_id"] for d in dev_docs}
    pub_ids = {json.loads(l)["doc_id"] for l in
               open(bundle / "vault" / "meta" / "documents.jsonl", encoding="utf-8")}
    excluded = dev_ids - pub_ids
    excluded_titles = [d["title"][:40] for d in dev_docs if d["doc_id"] in excluded]
    assert len(pub_ids) == manifest["n_docs_served"]

    # G8 re-check from the bundle's own files (exact set, independent of the builder's print)
    registered = {json.loads(l)["doc_id"] for l in
                  open(bundle / "curation" / "source_urls.jsonl", encoding="utf-8") if l.strip()}
    approved = set(json.loads((bundle / "curation" / "expected_null_source_urls.json")
                              .read_text(encoding="utf-8"))["doc_ids"])
    assert pub_ids - registered == approved, \
        f"null-URL set drift: {sorted((pub_ids - registered) ^ approved)}"
    print(f"[public-g8] null-URL set == approved ({len(approved)} docs) OK")
    readiness_file = Path(tempfile.mkdtemp()) / "readiness.json"

    # 5b. bundled code only (PYTHONPATH=<bundle>/src, cwd=bundle): the parent
    # process already imported the repository's src, so this must be a subprocess
    bundle_check = r'''
import sqlite3, sys
from rnd_factlink_mcp import tokenize_ko
from rnd_factlink_mcp.search_hybrid import bm25_ranking
from rnd_factlink_mcp.paths import VAULT
assert tokenize_ko.__file__.startswith(sys.argv[1]), tokenize_ko.__file__
top = bm25_ranking("137.6조원 규모", 8, "all")
assert top and top[0] == "d_7d310470a623_c0044", top[:3]
mem = sqlite3.connect(":memory:"); mem.execute("CREATE VIRTUAL TABLE t USING fts5(title, inst, body)")
con = sqlite3.connect(f"file:{VAULT / 'index' / 'lexical.db'}?mode=ro", uri=True)
vocab = set()
for (s,) in con.execute("SELECT text_norm FROM chunks"):
    vocab.update(tokenize_ko.search_tokens(s or "").split())
bad = unq = 0
for t in vocab:
    try: mem.execute("SELECT 1 FROM t WHERE t MATCH ?", ('"' + t.replace('"', '""') + '"',)).fetchall()
    except sqlite3.OperationalError: bad += 1
    try: mem.execute("SELECT 1 FROM t WHERE t MATCH ?", (t,)).fetchall()
    except sqlite3.OperationalError: unq += 1
print(f"[bundle-s1] anchor rank 1 OK; vocab {len(vocab)}: quoted parse errors {bad} (unquoted would fail {unq}) sqlite {sqlite3.sqlite_version}")
sys.exit(1 if bad or not unq else 0)
'''
    r = subprocess.run([sys.executable, "-c", bundle_check, str(bundle)],
                       env={**os.environ, "PYTHONPATH": str(bundle / "src")}, cwd=bundle,
                       capture_output=True, text=True, timeout=900)
    print(r.stdout.strip() or r.stderr.strip()[-800:])
    if r.returncode != 0:
        print("bundle-only S1 check FAILED")
        return 1

    env = {**os.environ,
           "PYTHONPATH": str(bundle / "src"),
           "PORT": str(PORT),
           "RND_FACTLINK_HTTP_HOST": "127.0.0.1",
           # smoke fires dozens of rapid calls from one IP — loosen the bucket
           # (guard behavior itself is covered by test_http.py)
           "RND_FACTLINK_RATE_CAPACITY": "300",
           "RND_FACTLINK_RATE_REFILL": "50",
           "RND_FACTLINK_EMBED_BACKEND":
               os.environ.get("RND_FACTLINK_EMBED_BACKEND", "local"),
           "RND_FACTLINK_READINESS_FILE": str(readiness_file)}
    proc = subprocess.Popen([sys.executable, "-m", "rnd_factlink_mcp.server", "--http"],
                            env=env, cwd=bundle, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        import httpx
        t0 = time.monotonic()
        while time.monotonic() - t0 < 90:
            if proc.poll() is not None:
                print(proc.stdout.read()[-2000:])
                print("server exited during boot")
                return 1
            try:
                # /readyz, not just an open socket: the guard answers MCP
                # requests 503 until the boot warm-up finishes (R1a)
                if httpx.get(f"http://127.0.0.1:{PORT}/readyz",
                             timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            print("server did not become ready")
            return 1
        asyncio.run(run_checks(bundle, excluded, excluded_titles))

        # 4b. readiness diagnostics (operator file, not /readyz) carry the registry digests
        diag = json.loads(readiness_file.read_text(encoding="utf-8")).get("diagnostics", {})
        assert diag.get("n_source_urls") == manifest["n_source_urls"], diag
        assert diag.get("n_null_source_url_docs") == len(approved), diag
        assert diag.get("url_registry_sha256") and diag.get("null_source_url_set_sha256")
        body = httpx.get(f"http://127.0.0.1:{PORT}/readyz", timeout=5.0).json()
        assert set(body) == {"status", "release"}, "public /readyz body must stay minimal"
        print("[public-r1b] readiness diagnostics in the operator file only; /readyz minimal OK")

        # 4c. real wire size of one tool result (content + structuredContent) — recorded
        r = httpx.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "search_rnd_facts",
                                             "arguments": {"question": OVERLAP_QUERIES[1],
                                                           "max_results": 8}}},
                       headers={"content-type": "application/json",
                                "accept": "application/json, text/event-stream"}, timeout=60.0)
        print(f"[public-r1b] wire size of a k=8 tool result: {len(r.content)} bytes (HTTP {r.status_code})")
    finally:
        proc.terminate()
        proc.wait(timeout=30)  # uvicorn graceful budget is 25s
    print("PUBLIC BUNDLE SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
