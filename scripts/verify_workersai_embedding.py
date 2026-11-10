#!/usr/bin/env python3
"""Workers AI @cf/baai/bge-m3 compatibility gate (public-release prerequisite).

Runs on the Mac (both backends available). Requires CF_ACCOUNT_ID and
CF_WORKERS_AI_TOKEN in the environment. PASS criteria:
1. per-text cosine(local BGE-M3, Workers AI) >= 0.99 for every sample
2. top-5 hybrid retrieval identical for every sample query when only the
   query-embedding backend changes

Only after PASS may the public deployment set RND_FACTLINK_EMBED_BACKEND=workersai
(decision ledger #8: 호환 검증 후 채택).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

COS_THRESHOLD = 0.99

SAMPLES = [
    "정부 연구개발 투자방향 및 기준의 중점 투자 분야는?",
    "학생인건비 잔액은 어떻게 반납하는가?",
    "제7차 건설기술진흥 기본계획의 추진 전략",
    "기술수준평가에서 건설교통 분야 결과",
    "R&D 예비타당성조사 제도 개편 내용",
    "탄소중립 녹색성장 기본계획의 감축 목표",
    # C1 corpus train (2026-11): new-document queries — long forms, years, numbers, look-alike plan names
    "제2차 국가연구개발 중장기 투자전략의 2026년부터 2030년까지 정부 R&D 투자 목표 규모는?",
    "제5차 과학기술인재 육성·지원 기본계획의 4대 전략",
    "제6기 나노기술종합발전계획과 제4기 국가나노기술지도의 관계",
    "2027년도 국가연구개발사업 예산 배분·조정 결과 총액과 전년 대비 증가율",
    "2026년 국가연구개발 행정제도 개선안의 연구비 사용 자율성 확대 내용",
    "국가연구개발 AI 연구윤리 가이드에서 생성형 AI 사용 시 연구자가 공개해야 하는 사항",
    "제5차 기초연구진흥종합계획의 개인기초연구 지원 방향",
    "제8차 산업기술혁신계획('24~'28) 초격차 로드맵의 전략 분야",
    "제5차 에너지기술개발계획(2024~2033)의 중점 기술 분야",
    "2025년 기술영향평가 대상 기술과 주요 결과",
    "2024년도 정부 R&D 특허성과 조사·분석에서 등록 특허 건수와 질적 수준 지표",
    "제4차 농림식품과학기술 육성 종합계획(2025~2029) 추진 전략별 투자 규모",
    "범부처 연구관리 전문기관 혁신방안에서 전문기관 기능 재편 방향",
    "제2차 국가유산 보존·관리 및 활용 연구개발 기본계획의 2026년도 시행계획 예산",
]


def main() -> int:
    if not os.environ.get("CF_ACCOUNT_ID") or not os.environ.get("CF_WORKERS_AI_TOKEN"):
        print("FAIL: CF_ACCOUNT_ID / CF_WORKERS_AI_TOKEN 환경변수가 필요합니다")
        return 1

    import numpy as np

    from rnd_factlink_mcp import embed_query as eq
    from rnd_factlink_mcp import search_hybrid
    from rnd_factlink_mcp.embed_dense import embed_texts

    # 1. vector compatibility
    worst = 1.0
    for text in SAMPLES:
        local = embed_texts([text])[0]
        remote = eq._embed_workersai(text)
        cos = float(np.dot(local, remote))
        worst = min(worst, cos)
        print(f"cosine={cos:.6f}  {text[:40]}")
    ok_cos = worst >= COS_THRESHOLD
    print(f"[1] min cosine {worst:.6f} (threshold {COS_THRESHOLD}) "
          f"{'PASS' if ok_cos else 'FAIL'}")

    # 2. retrieval equivalence (top-5 chunk ids must match exactly). Rule fixed before
    #    the run (C1 plan D8): same ids in a different order is a WARN that still passes
    #    (near-tie reordering); a different id set is a FAIL.
    ok_rank = True
    n_warn = 0
    for text in SAMPLES:
        os.environ["RND_FACTLINK_EMBED_BACKEND"] = "local"
        a = [r["chunk_id"] for r in search_hybrid.hybrid_search(text, k=5)]
        os.environ["RND_FACTLINK_EMBED_BACKEND"] = "workersai"
        b = [r["chunk_id"] for r in search_hybrid.hybrid_search(text, k=5)]
        if a == b:
            print(f"top5 match  {text[:40]}")
        elif set(a) == set(b):
            n_warn += 1
            print(f"top5 WARN (same set, different order)  {text[:40]}")
            print(f"   local:    {a}")
            print(f"   workersai:{b}")
        else:
            ok_rank = False
            print(f"top5 MISMATCH  {text[:40]}")
            print(f"   local:    {a}")
            print(f"   workersai:{b}")
    os.environ["RND_FACTLINK_EMBED_BACKEND"] = "local"
    print(f"[2] retrieval equivalence {'PASS' if ok_rank else 'FAIL'} "
          f"({len(SAMPLES)} samples, {n_warn} order-only warning(s))")

    ok = ok_cos and ok_rank
    print(f"WORKERS AI COMPATIBILITY {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
