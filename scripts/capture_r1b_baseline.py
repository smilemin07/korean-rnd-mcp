#!/usr/bin/env python3
"""Re-capture the search-invariance baseline after a CORPUS train (not after a code change).

test_r1b_invariance.py compares retrieval against a sealed baseline bound to one index
snapshot. A corpus train changes the snapshot, so the baseline must be re-captured — with
the retrieval code frozen — by replaying the same (question, k, filter) sets. The capture
itself proves nothing; the evidence that the code is unchanged comes from (a) git diff of
the search/embedding modules, (b) a PASS of the previous baseline on the pre-train index,
and (c) the per-query diff between the old and new baselines being explained by the
corpus change alone. Only then is the new file's sha pinned in test_r1b_invariance.py.

Usage: .venv/bin/python scripts/capture_r1b_baseline.py <previous_baseline.json> <out.json>
"""
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _raw_row_view(rows: list[dict]) -> list[dict]:
    # identical projection to test_r1b_invariance.raw_row_view
    return [{"chunk_id": r["chunk_id"], "rrf": r["rrf_score"], "sparse_rank": r["sparse_rank"],
             "dense_rank": r["dense_rank"], "dense_score": r["dense_score"]} for r in rows]


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout.strip()


async def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    prev = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    import fastmcp
    import kiwipiepy
    import numpy
    from fastmcp import Client

    from rnd_factlink_mcp.server import mcp
    from rnd_factlink_mcp.search_hybrid import hybrid_search, verify_snapshot_consistency

    raw_rows = []
    for c in prev["raw_rows"]:
        rows = hybrid_search(c["question"], k=c["k"], plan_status_filter=c["filter"])
        raw_rows.append({"filter": c["filter"], "k": c["k"], "question": c["question"],
                         "rows": _raw_row_view(rows)})
    cases = []
    async with Client(mcp) as cl:
        for c in prev["cases"]:
            resp = (await cl.call_tool("search_rnd_facts", {
                "question": c["question"], "max_results": c["max_results"],
                "plan_status_filter": c["plan_status_filter"]})).data
            cases.append({"question": c["question"], "max_results": c["max_results"],
                          "plan_status_filter": c["plan_status_filter"], "response": resp})
    prov = {
        "captured_from": f"replay of {Path(sys.argv[1]).name} question/k/filter sets on the current index",
        "dense_meta": json.loads((ROOT / "vault/index/dense/meta.json").read_text(encoding="utf-8")),
        "documents_sha256": _sha(ROOT / "vault/meta/documents.jsonl"),
        "pages_sha256": _sha(ROOT / "vault/meta/pages.jsonl"),
        "source_urls_sha256": _sha(ROOT / "curation/source_urls.jsonl"),
        "embed_backend": "local", "fastmcp": fastmcp.__version__, "numpy": numpy.__version__,
        "kiwipiepy": kiwipiepy.__version__, "python": platform.python_version(),
        "git_head": _git("rev-parse", "HEAD"), "git_dirty": _git("status", "--short").splitlines(),
        "snapshot_fingerprint": verify_snapshot_consistency(),
    }
    out.write_text(json.dumps({"cases": cases, "provenance": prov, "raw_rows": raw_rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}: {len(cases)} cases, {len(raw_rows)} raw sets, "
          f"snapshot {prov['snapshot_fingerprint']}, sha256 {_sha(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
