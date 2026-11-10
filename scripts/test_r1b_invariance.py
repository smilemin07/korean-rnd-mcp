#!/usr/bin/env python3
"""R1b deployment gate — search invariance (UPDATE-PLAN-R1 §11).

R1b only ADDS fields to the search_rnd_facts response; the retrieval itself must
be untouched. "Same number of results" is not enough (the same count can hide a
different chunk, order, excerpt or citation), so this compares the pre-R1b
projection of every response bit-for-bit against a baseline captured from the
pre-change code (git 0a789e0c) with the same queries, k and filters.

Baseline: vault/derived/reports/r1b_invariance_baseline.json (git-untracked; it
was captured BEFORE any R1b code change — re-capturing after the change would
make this gate circular). Queries are non-golden on purpose: sealed evaluation
items must never appear in the public repository.

Usage: .venv/bin/python scripts/test_r1b_invariance.py [baseline.json]
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

BASELINE = ROOT / "vault" / "derived" / "reports" / "r1b_invariance_baseline_v6.json"
# Pinned identity of the sealed baseline (UPDATE-PLAN-R1 §12 D-C, §16 G-a): a
# shrunken or swapped baseline would pass every comparison below with 0
# mismatches, so the digest and the case counts are enforced here, not by hand.
BASELINE_SHA256 = "7cd16bdb7218cda99e7170f9bb7ab51c9a0c7a1545a51e0b3f346f9e8112291a"  # v6: 6 docs excluded, 1 title change (2026-11-10)
EXPECTED_CASES = 90

# Everything R1b adds. Stripping exactly these must give back the v2 projection.
NEW_TOP_LEVEL = {"retrieval_semantics", "answer_requirements", "answer_requirements_note",
                 "candidate_source_manifest", "locator_semantics", "response_contract_revision"}
NEW_CITATION = {"variant_label", "authority_label"}   # natural-language labels (D-F)
NEW_PLAN = {"status_label", "statutory_label"}        # inside citation.plan


def legacy_projection(resp: dict) -> dict:
    out = {k: v for k, v in resp.items() if k not in NEW_TOP_LEVEL}
    if "results" in out:
        res = []
        for r in out["results"]:
            cite = {k: v for k, v in r["citation"].items() if k not in NEW_CITATION}
            if isinstance(cite.get("plan"), dict):
                cite["plan"] = {k: v for k, v in cite["plan"].items() if k not in NEW_PLAN}
            res.append({**r, "citation": cite})
        out["results"] = res
    return out


def raw_row_view(rows: list[dict]) -> list[dict]:
    return [{"chunk_id": r["chunk_id"], "rrf": r["rrf_score"], "sparse_rank": r["sparse_rank"],
             "dense_rank": r["dense_rank"], "dense_score": r["dense_score"]} for r in rows]


def canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


async def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else BASELINE
    base = json.loads(path.read_text(encoding="utf-8"))
    # Layer -1 — the baseline itself (only the sealed default file is pinned)
    import hashlib
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if path == BASELINE and digest != BASELINE_SHA256:
        print(f"BASELINE MISMATCH: sha256 {digest[:12]} != pinned {BASELINE_SHA256[:12]}")
        return 1
    n_raw, n_cases = len(base.get("raw_rows", [])), len(base.get("cases", []))
    if n_raw != EXPECTED_CASES or n_cases != EXPECTED_CASES:
        print(f"BASELINE MISMATCH: {n_raw} raw sets / {n_cases} tool cases, expected {EXPECTED_CASES} each")
        return 1
    from fastmcp import Client
    from rnd_factlink_mcp.server import mcp
    from rnd_factlink_mcp.search_hybrid import hybrid_search, verify_snapshot_consistency
    mismatches = 0
    # Layer 0 — provenance: the comparison is only meaningful on the same index
    # snapshot with the same (local, deterministic) embedding backend.
    prov = base.get("provenance", {})
    fp = verify_snapshot_consistency()
    if prov.get("snapshot_fingerprint") and prov["snapshot_fingerprint"] != fp:
        print(f"PROVENANCE MISMATCH: baseline snapshot {prov['snapshot_fingerprint']} != current {fp}")
        return 1
    import os
    if os.environ.get("RND_FACTLINK_EMBED_BACKEND", "local") != prov.get("embed_backend", "local"):
        print("PROVENANCE MISMATCH: embed backend differs from the baseline capture")
        return 1
    # Layer 1 — raw retrieval rows (ids, order, fused score, per-leg ranks/scores).
    # The server never exposes these, so the tool-response layer alone cannot prove them.
    for case in base.get("raw_rows", []):
        rows = raw_row_view(hybrid_search(case["question"], k=case["k"],
                                          plan_status_filter=case["filter"]))
        if canon(rows) != canon(case["rows"]):
            mismatches += 1
            print(f"RAW MISMATCH k={case['k']} f={case['filter']} {case['question'][:34]!r}")
    print(f"layer 1 (raw rows): {len(base.get('raw_rows', []))} sets checked")
    # Layer 2 — full tool response minus the R1b additions.
    async with Client(mcp) as c:
        for case in base["cases"]:
            resp = (await c.call_tool("search_rnd_facts", {
                "question": case["question"], "max_results": case["max_results"],
                "plan_status_filter": case["plan_status_filter"]})).data
            new = legacy_projection(resp)
            old = legacy_projection(case["response"])  # baseline may predate the new keys
            if canon(new) != canon(old):
                mismatches += 1
                old_ids = [r["chunk_id"] for r in old.get("results", [])]
                new_ids = [r["chunk_id"] for r in new.get("results", [])]
                print(f"MISMATCH k={case['max_results']} f={case['plan_status_filter']} "
                      f"{case['question'][:34]!r}\n   old={old_ids}\n   new={new_ids}")
                if old_ids == new_ids:
                    for o, n in zip(old["results"], new["results"]):
                        if canon(o) != canon(n):
                            print(f"   differs in chunk {o['chunk_id']}: "
                                  f"{[k for k in o if canon(o.get(k)) != canon(n.get(k))]}")
                    for k in set(old) | set(new):
                        if k != "results" and canon(old.get(k)) != canon(new.get(k)):
                            print(f"   top-level differs: {k}")
    head = base.get("git_head") or prov.get("git_head", "?")
    print(f"baseline {path.name} (HEAD {head[:8]}): {len(base['cases'])} tool cases, "
          f"{mismatches} mismatch(es) across both layers")
    if mismatches:
        print("SEARCH INVARIANCE GATE FAIL")
        return 1
    print("SEARCH INVARIANCE GATE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
