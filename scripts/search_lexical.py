#!/usr/bin/env python3
"""Dev search CLI. Usage: search_lexical.py "query" [--mode kiwi|tri|both] [--k N]"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnd_factlink_mcp.index_lexical import search


def show(rows, label):
    print(f"== {label} ==")
    for i, r in enumerate(rows, 1):
        loc = f"PDF p.{r['page_start']+1}" if r["page_start"] is not None else r["locator"]
        if r["page_start"] is not None and r["page_end"] != r["page_start"]:
            loc += f"-{r['page_end']+1}"
        snippet = " ".join(r["text_raw"].split())[:90]
        print(f"{i:2d}. [{r['variant_type']}/t{r['authority_tier']}] {r['title'][:46]} | {loc} | {snippet}")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--mode", default="kiwi", choices=["kiwi", "tri", "both"])
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()
    if args.mode in ("kiwi", "both"):
        show(search(args.query, "kiwi", args.k), "kiwi+FTS5 BM25")
    if args.mode in ("tri", "both"):
        show(search(args.query, "tri", args.k), "trigram baseline")
