#!/usr/bin/env python3
"""Dev hybrid search CLI (raw scores shown here only — never in MCP responses).

Usage: search_hybrid.py "query" [--mode hybrid|bm25|dense] [--k N]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnd_factlink_mcp.search_hybrid import extraction_gaps, hybrid_search

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "bm25", "dense"])
    ap.add_argument("--k", type=int, default=8)
    args = ap.parse_args()
    rows = hybrid_search(args.query, k=args.k, mode=args.mode)
    for g in extraction_gaps(args.query):
        print(f"GAP: [{g['gap_type']}] {g['title'][:50]} — {g['reason']}")
    for i, r in enumerate(rows, 1):
        loc = f"PDF p.{r['page_start']+1}" if r["page_start"] is not None else r["locator"]["type"]
        print(f"{i:2d}. rrf={r['rrf_score']:.4f} s={r['sparse_rank']} d={r['dense_rank']}"
              f" cos={r['dense_score']} [{r['variant_type']}/t{r['authority_tier']}]"
              f" {r['title'][:42]} | {loc} | {' '.join(r['text_raw'].split())[:70]}")
