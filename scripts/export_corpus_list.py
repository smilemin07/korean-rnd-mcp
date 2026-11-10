#!/usr/bin/env python3
"""Regenerate docs/CORPUS.md — the public list of served documents.

Applies the same serving rule as build_public_bundle.py (tier 3 and the
denylist excluded) to the dev documents.jsonl, so the list matches what the
public bundle serves. Run after every corpus change, before committing.
"""

import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from rnd_factlink_mcp.paths import REPO_ROOT, VAULT  # noqa: E402

OUT = REPO_ROOT / "docs" / "CORPUS.md"
VARIANT_KO = {"original": "원문", "press_release": "보도자료", "summary": "요약본(공식)",
              "notice": "고시문", "full_text": "전문", "excerpt": "발췌"}
STATUS_KO = {"current": "현행", "superseded": "구판", "expired": "만료", "unknown": "미확인"}


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    docs = _jsonl(VAULT / "meta" / "documents.jsonl")
    denied = {r["doc_id"] for r in _jsonl(REPO_ROOT / "curation" / "serving_denylist.jsonl")}
    urls = {r["doc_id"]: r["source_url"] for r in _jsonl(REPO_ROOT / "curation" / "source_urls.jsonl")
            if r.get("source_url")}
    served = [d for d in docs if d.get("authority_tier") != 3 and d["doc_id"] not in denied]
    by_inst = defaultdict(list)
    for d in served:
        by_inst[d.get("institution_normalized") or "(기관 미표기)"].append(d)

    lines = [f"# 코퍼스 목록 ({date.today().isoformat()} 기준, {len(served)}건)", "",
             "korean-rnd-mcp가 검색 대상으로 제공하는 문서 전체입니다. "
             "`scripts/export_corpus_list.py`로 자동 생성되며 코퍼스 변경 시 갱신됩니다. "
             "날짜는 파일명 유래(발간일과 다를 수 있음). 계획 상태는 운영자 큐레이션 기준.", ""]
    for inst in sorted(by_inst, key=lambda k: (-len(by_inst[k]), k)):
        items = sorted(by_inst[inst], key=lambda d: (d.get("filename_date") or "", d["title"]))
        lines += [f"## {inst} ({len(items)}건)", "",
                  "| 날짜 | 문서 | 구분 | 계획 상태 | 원문 |", "|---|---|---|---|---|"]
        for d in items:
            status = STATUS_KO.get(d.get("plan_status"), "") if d.get("plan_id") else ""
            url = urls.get(d["doc_id"])
            lines.append(f"| {d.get('filename_date') or ''} | {d['title']} | "
                         f"{VARIANT_KO.get(d['variant_type'], d['variant_type'])} | {status} | "
                         f"{'[링크](' + url + ')' if url else ''} |")
        lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(served)} docs, {sum(1 for d in served if d['doc_id'] in urls)} with URL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
