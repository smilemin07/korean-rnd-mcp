#!/usr/bin/env python3
"""Public release bundle builder (adversarially reviewed design, 2026-08-21).

Produces dist/bundle-<UTC ts>/ mirroring the repo layout (src/, vault/,
curation/, models/) so the bundled code resolves every path inside the bundle
— the container and the local smoke test run exactly the same tree.

Serving rule (README.md "코퍼스 > 제공 기준"): serve everything EXCEPT
- authority_tier == 3 (personal summaries; rule-based, applied here)
- curation/serving_denylist.jsonl (rights-based, sha-bound)
- KOGL type 2/4 marks (commercial-use restriction) — gated via the scan record

Gates (ALL must pass or the bundle is deleted):
 G1 denylist binding: every entry matches exactly one doc by doc_id+sha256
 G2 KOGL scan coverage: every served pdf doc has a scan verdict; no
    type_2/type_4/type_unknown among served docs
 G3 lexical build gate (public corpus only — its own FTS statistics)
 G4 lexical/dense chunk-id fingerprints identical
 G5 exclusion: no excluded doc_id anywhere in the bundle artifacts
 G6 chunk integrity: every public chunk's text sha256 equals the dev index's
 G7 relation closure: no relation_target pointing outside the served set
    (dangling references are nulled and counted, >0 reported)
 G8 null-URL set (R1b, UPDATE-PLAN-R1 §12 D-H): served docs without a registered
    source_url must equal the approved set in curation/expected_null_source_urls.json
    — exact match, so a damaged registry (silent null growth) or an unreviewed
    URL change stops the release before transfer
"""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np

from rnd_factlink_mcp import index_lexical
from rnd_factlink_mcp.paths import REPO_ROOT, VAULT
from rnd_factlink_mcp.readiness import compute_release_id

DENYLIST = REPO_ROOT / "curation" / "serving_denylist.jsonl"
KOGL_SCAN = REPO_ROOT / "curation" / "kogl_scan.jsonl"
SOURCE_URLS = REPO_ROOT / "curation" / "source_urls.jsonl"
EXPECTED_NULLS = REPO_ROOT / "curation" / "expected_null_source_urls.json"

# fields the server actually reads — everything else (source_refs, source_path,
# source_folder, ...) is internal and stays out of the public bundle
DOC_KEEP = ["doc_id", "title", "institution_raw", "institution_normalized",
            "filename_date", "date_semantics", "variant_type", "authority_tier",
            "evidence_eligible", "family_id", "relation_type", "relation_target",
            "ext", "n_pages", "page_status_counts", "rights_review_status",
            "metadata_parse_status", "content_sha256",
            "plan_id", "plan_ids", "plan_status", "plan_status_as_of",
            "current_basis", "statutory_status", "statutory_basis",
            "plan_period", "announced_on", "plan_ministry", "rnd_level"]
PAGE_KEEP = ["doc_id", "pdf_page_index", "printed_page_number", "printed_page_confidence"]

KOGL_BLOCKED = {"kogl_type_2", "kogl_type_4", "kogl_type_unknown"}


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def fail(msg: str, bundle: Path | None = None) -> int:
    print(f"GATE FAIL: {msg}")
    if bundle and bundle.exists():
        shutil.rmtree(bundle)
        print(f"bundle removed: {bundle}")
    print("PUBLIC BUNDLE FAIL")
    return 1


def main() -> int:
    t0 = time.perf_counter()
    docs = _jsonl(VAULT / "meta" / "documents.jsonl")
    by_id = {d["doc_id"]: d for d in docs}

    # G1 — denylist sha binding
    denylist = _jsonl(DENYLIST)
    for e in denylist:
        d = by_id.get(e["doc_id"])
        if d is None or d.get("content_sha256") != e["content_sha256"]:
            return fail(f"denylist entry unbound: {e['doc_id']}")
    denied = {e["doc_id"] for e in denylist}
    print(f"[G1] denylist bound: {len(denied)} docs")

    # serving allowlist
    tier3 = {d["doc_id"] for d in docs if d.get("authority_tier") == 3}
    for d in docs:  # schema sanity (type check on the tier field)
        if not isinstance(d.get("authority_tier"), int):
            return fail(f"non-integer authority_tier on {d['doc_id']}")
    allowed = {d["doc_id"] for d in docs} - tier3 - denied

    # G2 — KOGL scan coverage over served pdf docs
    if not KOGL_SCAN.exists():
        return fail(f"{KOGL_SCAN} missing — run the KOGL scan first")
    scan = {r["doc_id"]: r for r in _jsonl(KOGL_SCAN)}
    unscanned = [i for i in sorted(allowed) if by_id[i]["ext"] == "pdf" and i not in scan]
    if unscanned:
        return fail(f"KOGL scan missing for served docs: {unscanned[:5]} (+{max(0, len(unscanned)-5)})")
    blocked = [i for i in sorted(allowed) if scan.get(i, {}).get("verdict") in KOGL_BLOCKED]
    if blocked:
        return fail(f"KOGL-restricted docs still in allowlist — add to denylist or resolve: {blocked}")
    print(f"[G2] KOGL scan coverage OK ({len(scan)} records, 0 blocked among served)")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = REPO_ROOT / "dist" / f"bundle-{ts}"
    (bundle / "vault" / "index" / "dense").mkdir(parents=True)
    (bundle / "vault" / "meta").mkdir(parents=True)
    (bundle / "vault" / "derived" / "reports").mkdir(parents=True)
    (bundle / "curation").mkdir(parents=True)

    # G3 — public lexical index (its own corpus statistics)
    rc = index_lexical.build(doc_allowlist=allowed,
                             db_path=bundle / "vault" / "index" / "lexical.db")
    if rc != 0:
        return fail("public lexical build gate failed", bundle)

    pub_con = sqlite3.connect(bundle / "vault" / "index" / "lexical.db")
    pub_rows = pub_con.execute(
        "SELECT chunk_id, doc_id, source_folder FROM chunks ORDER BY rowid").fetchall()
    pub_ids = [r[0] for r in pub_rows]
    if any(r[1] not in allowed for r in pub_rows):
        return fail("excluded doc chunk in public lexical.db", bundle)
    if any(r[2] is not None for r in pub_rows):
        return fail("source_folder not redacted in public lexical.db", bundle)

    # dense filter — rows selected by chunk_id from the dev snapshot
    dev_ids = json.loads((VAULT / "index" / "dense" / "chunk_ids.json").read_text(encoding="utf-8"))
    if len(set(dev_ids)) != len(dev_ids):
        return fail("dev dense chunk_ids not unique", bundle)
    pos = {cid: i for i, cid in enumerate(dev_ids)}
    missing = [cid for cid in pub_ids if cid not in pos]
    if missing:
        return fail(f"public chunk missing from dev dense index: {missing[:3]}", bundle)
    dev_vecs = np.load(VAULT / "index" / "dense" / "embeddings.npy", mmap_mode="r")
    out_vecs = np.ascontiguousarray(dev_vecs[[pos[cid] for cid in pub_ids]])
    with open(bundle / "vault" / "index" / "dense" / "embeddings.npy", "wb") as f:
        np.save(f, out_vecs)
    (bundle / "vault" / "index" / "dense" / "chunk_ids.json").write_text(
        json.dumps(pub_ids), encoding="utf-8")
    dev_meta = json.loads((VAULT / "index" / "dense" / "meta.json").read_text(encoding="utf-8"))
    (bundle / "vault" / "index" / "dense" / "meta.json").write_text(json.dumps({
        **dev_meta, "n": len(pub_ids), "public_bundle": ts,
        "filtered_from": hashlib.sha256("\n".join(dev_ids).encode()).hexdigest()[:12],
    }), encoding="utf-8")

    # G4 — fingerprints (same construction, but verify anyway)
    fp_lex = hashlib.sha256("\n".join(pub_ids).encode()).hexdigest()
    fp_dense = hashlib.sha256("\n".join(json.loads(
        (bundle / "vault" / "index" / "dense" / "chunk_ids.json").read_text())).encode()).hexdigest()
    if fp_lex != fp_dense:
        return fail("lexical/dense fingerprint mismatch", bundle)
    print(f"[G4] snapshot fingerprint {fp_lex[:12]} ({len(pub_ids)} chunks)")

    # G6 — chunk text integrity vs dev index (all chunks)
    dev_con = sqlite3.connect(VAULT / "index" / "lexical.db")
    dev_sha = {r[0]: hashlib.sha256(r[1].encode()).hexdigest()
               for r in dev_con.execute("SELECT chunk_id, text_raw FROM chunks")}
    dev_con.close()
    for cid, sha_pub in ((r[0], hashlib.sha256(r[1].encode()).hexdigest()) for r in
                         pub_con.execute("SELECT chunk_id, text_raw FROM chunks")):
        if dev_sha.get(cid) != sha_pub:
            return fail(f"chunk text mismatch dev↔public: {cid}", bundle)
    pub_con.close()
    print(f"[G6] chunk text integrity OK ({len(pub_ids)} chunks)")

    # public documents.jsonl / pages.jsonl (+ G7 relation closure)
    dangling = 0
    with open(bundle / "vault" / "meta" / "documents.jsonl", "w", encoding="utf-8") as f:
        for d in docs:
            if d["doc_id"] not in allowed:
                continue
            pub = {k: d.get(k) for k in DOC_KEEP}
            pub["servable_scope"] = "public_alpha"
            if pub.get("relation_target") and pub["relation_target"] not in allowed:
                pub["relation_target"] = None
                pub["relation_type"] = None
                dangling += 1
            f.write(json.dumps(pub, ensure_ascii=False) + "\n")
    with open(bundle / "vault" / "meta" / "pages.jsonl", "w", encoding="utf-8") as f:
        for p in _jsonl(VAULT / "meta" / "pages.jsonl"):
            if p["doc_id"] in allowed:
                f.write(json.dumps({k: p.get(k) for k in PAGE_KEEP}, ensure_ascii=False) + "\n")
    print(f"[G7] relation closure: {dangling} dangling reference(s) nulled")

    # filtered source_urls (doc_id + url only — no operational notes)
    with open(bundle / "curation" / "source_urls.jsonl", "w", encoding="utf-8") as f:
        n_urls = 0
        registered: set[str] = set()
        for r in _jsonl(SOURCE_URLS):
            if r["doc_id"] in allowed and r.get("source_url"):
                f.write(json.dumps({"doc_id": r["doc_id"],
                                    "source_url": r["source_url"]}, ensure_ascii=False) + "\n")
                n_urls += 1
                registered.add(r["doc_id"])

    # G8 — served docs without a URL == approved set (exact, from the bundle's own files)
    if not EXPECTED_NULLS.exists():
        return fail(f"{EXPECTED_NULLS} missing — approve the null-URL set first", bundle)
    expected_nulls = set(json.loads(EXPECTED_NULLS.read_text(encoding="utf-8"))["doc_ids"])
    actual_nulls = allowed - registered
    if actual_nulls != expected_nulls:
        return fail(f"null-URL set drift: unexpected={sorted(actual_nulls - expected_nulls)} "
                    f"missing={sorted(expected_nulls - actual_nulls)}", bundle)
    shutil.copy2(EXPECTED_NULLS, bundle / "curation" / "expected_null_source_urls.json")
    print(f"[G8] null-URL set matches approval ({len(actual_nulls)} docs)")

    # code + tokenizer + runtime deps
    shutil.copytree(REPO_ROOT / "src" / "rnd_factlink_mcp",
                    bundle / "src" / "rnd_factlink_mcp",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (bundle / "models" / "bge-m3").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "models" / "bge-m3" / "tokenizer.json",
                 bundle / "models" / "bge-m3" / "tokenizer.json")
    for name in ("Dockerfile", "requirements-runtime.txt"):
        if (REPO_ROOT / name).exists():
            shutil.copy2(REPO_ROOT / name, bundle / name)

    # G5 — excluded doc_ids absent from every bundle artifact
    excluded = (tier3 | denied)
    for rel in ("vault/meta/documents.jsonl", "vault/meta/pages.jsonl",
                "curation/source_urls.jsonl"):
        text = (bundle / rel).read_text(encoding="utf-8")
        leak = [i for i in excluded if i in text]
        if leak:
            return fail(f"excluded doc_id in {rel}: {leak[:3]}", bundle)
    con = sqlite3.connect(bundle / "vault" / "index" / "lexical.db")
    leak = [i for i in excluded if con.execute(
        "SELECT 1 FROM chunks WHERE doc_id = ? LIMIT 1", (i,)).fetchone()]
    con.close()
    if leak:
        return fail(f"excluded doc_id in public lexical.db: {leak}", bundle)
    if any(cid.rsplit("_c", 1)[0] in excluded for cid in pub_ids):
        return fail("excluded doc chunk in dense ids", bundle)
    print(f"[G5] exclusion verified: {len(excluded)} excluded docs absent everywhere")

    # release manifest
    git = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    import importlib.metadata as im
    files = sorted(p for p in bundle.rglob("*") if p.is_file() and p.name != "release_manifest.json")
    manifest = {
        "bundle": ts, "git_commit": git,
        "snapshot_fingerprint": fp_lex[:12],
        "n_docs_served": len(allowed), "n_chunks": len(pub_ids),
        "n_excluded": {"tier3": len(tier3), "denylist": len(denied)},
        "n_source_urls": n_urls, "n_null_source_urls": len(actual_nulls),
        "dangling_relations_nulled": dangling,
        "kiwipiepy": im.version("kiwipiepy"),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": {str(p.relative_to(bundle)): _sha(p) for p in files},
    }
    # Opaque release id over the per-file digest map: code-only changes move it,
    # and rebuilding the same bundle under a new mutable image tag does not. The
    # server recomputes it from the same map rather than trusting this field, so
    # the two can never disagree (rnd_factlink_mcp.readiness.release_id).
    manifest["release_id"] = compute_release_id(manifest)
    (bundle / "release_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"served {len(allowed)} docs / {len(pub_ids)} chunks | urls {n_urls} | "
          f"excluded tier3 {len(tier3)} + denylist {len(denied)} | "
          f"{time.perf_counter()-t0:.0f}s")
    print(f"release_id {manifest['release_id']} — record this for the cutover script")
    print(f"PUBLIC BUNDLE PASS: {bundle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
