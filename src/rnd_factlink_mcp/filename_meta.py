"""Filename-convention metadata parsing and document-family grouping.

Corpus filenames follow: 「유형 - YYYY-MM[-DD] - [분류 - ] 「기관」 - 제목.ext」
with variant suffixes like (요약), (보도자료), (고시문), (전문).

Rules per docs/SCHEMA.md:
- filename date is stored as filename_date only (never auto-promoted to publication_date)
- parse failure is an explicit state (metadata_parse_status), not an error
- family grouping key = year + normalized core title; ambiguous cases are
  resolved via the manual FAMILY_OVERRIDES table (reviewed by hand once)
"""

import re
import unicodedata

# strip these title suffixes to get the family core title
VARIANT_SUFFIXES = [
    ("(요약)", "summary"),
    ("(보도자료)", "press_release"),
    ("(보도참고)", "press_release"),
    ("(고시문)", "notice"),
    ("(전문)", "full_text"),
]

# parenthetical year ranges vary across variants of the same doc: (2023-2027), (21-30), (2026-2030)
YEAR_RANGE_RE = re.compile(r"\((?:\d{2,4}\s*[-~]\s*\d{2,4})\)")
# Numbered sub-volumes (" - 세부 - 01 건설교통") are SEPARATE subject-matter
# documents, not variants of the main volume, so they keep their identity in the
# family core and become their own family. The earlier rule stripped this suffix,
# which fused 11 unrelated volumes into one family and let the family cap starve
# results (docs/SCHEMA.md §10-§11). The pattern is deliberately narrow (dash,
# 세부, dash, two digits, subject name); ingest QA prints every hit for review.
SUB_VOLUME_RE = re.compile(r"\s*-\s*세부\s*-\s*\d{2}\s+\S+")

DATE_RE = r"\d{4}(?:-\d{2})?(?:-\d{2})?"

# cascade: with category segment / without / no institution
_PATTERNS = [
    re.compile(rf"^(?P<type>[^-]+?) - (?P<date>{DATE_RE}) - (?P<cat>[^「]+?) - 「(?P<inst>[^」]+)」 - (?P<title>.+)$"),
    re.compile(rf"^(?P<type>[^-]+?) - (?P<date>{DATE_RE}) - 「(?P<inst>[^」]+)」 - (?P<title>.+)$"),
    re.compile(rf"^(?P<type>[^-]+?) - (?P<date>{DATE_RE}) - (?P<title>.+)$"),
]

# only unambiguous standard abbreviations; everything else passes through as-is
# (국과심 is deliberately NOT aliased: 국가과학기술심의회/자문회의 변천으로 시기별 의미가 다름)
INSTITUTION_ALIASES = {
    "과기부": "과학기술정보통신부",
    "국토부": "국토교통부",
    "해수부": "해양수산부",
    "행안부": "행정안전부",
    "고용노동부": "고용노동부",
    "키스텝": "한국과학기술기획평가원(KISTEP)",
    "농식품부": "농림축산식품부",
    "복지부": "보건복지부",
    "산업부": "산업통상자원부",
    "문체부": "문화체육관광부",
    "방사청": "방위사업청",
    "식약처": "식품의약품안전처",
    "원안위": "원자력안전위원회",
    "예정처": "국회예산정책처",
    # 예산처/기획처 문서 2건 모두 mpb.go.kr(기획예산처) 배포본으로 URL 확인됨 (2026-08-20)
    "예산처": "기획예산처",
    "기획처": "기획예산처",
    "KAIA": "국토교통과학기술진흥원(KAIA)",
    "복지부·과기부": "보건복지부·과학기술정보통신부",
    "국토부, 해수부": "국토교통부, 해양수산부",
}

# manual family-core overrides, keyed by filename stem (reviewed 2026-08-09).
# Value = the core title to group under (and optional variant override).
FAMILY_OVERRIDES = {
    # "세부 실천과제 내역" does NOT match the numbered sub-volume pattern, but the
    # override predates that rule and stays as the explicit record of the decision.
    "정책 - 2026-06-26 - 「국과심」 - 제6차 과학기술기본계획(2026-2030) - 세부 실천과제 내역":
        {"core": "제6차 과학기술기본계획", "variant": "excerpt"},
}


def parse_filename(stem: str, ext: str) -> dict:
    """Parse a filename stem (no extension) into metadata fields."""
    stem = unicodedata.normalize("NFC", stem.strip())
    meta = {
        "doc_type_raw": None,
        "filename_date": None,
        "date_semantics": "filename_only",
        "category_raw": None,
        "institution_raw": None,
        "institution_normalized": None,
        "title": stem,
        "variant_type": "original",
        "family_core": None,
        "metadata_parse_status": "failed",
    }
    for i, pat in enumerate(_PATTERNS):
        m = pat.match(stem)
        if not m:
            continue
        g = m.groupdict()
        meta["doc_type_raw"] = g["type"].strip()
        meta["filename_date"] = g["date"]
        meta["category_raw"] = g.get("cat", "").strip() or None if "cat" in g else None
        inst = g.get("inst")
        if inst:
            inst = inst.strip()
            meta["institution_raw"] = inst
            meta["institution_normalized"] = INSTITUTION_ALIASES.get(inst, inst)
        meta["title"] = g["title"].strip()
        meta["metadata_parse_status"] = "ok" if inst else "partial"
        break

    # variant detection on the title tail
    title = meta["title"]
    core = title
    for suffix, variant in VARIANT_SUFFIXES:
        if core.endswith(suffix):
            meta["variant_type"] = variant
            core = core[: -len(suffix)].strip()
            break
    # numbered sub-volumes deliberately KEEP their suffix in the core: each is a
    # separate subject-matter document and must form its own family
    meta["is_sub_volume"] = bool(SUB_VOLUME_RE.search(core))
    core = YEAR_RANGE_RE.sub("", core).strip()

    ov = FAMILY_OVERRIDES.get(stem)
    if ov:
        core = ov["core"]
        if "variant" in ov:
            meta["variant_type"] = ov["variant"]
    meta["family_core"] = core

    # authority tier by source form (md/html are operator-authored or generated summaries)
    if ext in ("md", "html"):
        meta["authority_tier"] = 3
        meta["evidence_eligible"] = False
    elif meta["variant_type"] == "press_release":
        meta["authority_tier"] = 2
        meta["evidence_eligible"] = True
    else:
        meta["authority_tier"] = 1
        meta["evidence_eligible"] = True
    return meta


def family_key(meta: dict) -> str:
    year = (meta["filename_date"] or "0000")[:4]
    return f"{year}|{meta['family_core']}"


if __name__ == "__main__":
    # self-test on tricky filename shapes (real public titles; 가상 = synthetic stand-ins)
    cases = [
        ("정책 - 2026-03-12 - 「과기부」 - 2027년도 국가연구개발 투자방향 및 기준", "pdf",
         dict(institution_raw="과기부", institution_normalized="과학기술정보통신부",
              filename_date="2026-03-12", variant_type="original",
              family_core="2027년도 국가연구개발 투자방향 및 기준", metadata_parse_status="ok")),
        ("정책 - 2026-03 - 「예산처」 - 27년 예산안 편성지침", "pdf",
         dict(filename_date="2026-03", institution_raw="예산처", institution_normalized="기획예산처")),
        ("보고서 - 2026-02-05 - 과학기술 - 「키스텝」 - 2024년도 기술수준평가 결과(안) - 세부 - 01 건설교통", "pdf",
         dict(category_raw="과학기술", variant_type="original", is_sub_volume=True,
              family_core="2024년도 기술수준평가 결과(안) - 세부 - 01 건설교통")),
        ("정책 - 2026-04-15 - 「국제 - 가상주제」 - 가상 국제협약 메모", "md",
         dict(institution_raw="국제 - 가상주제", authority_tier=3, evidence_eligible=False)),
        ("논문 - 2020-01-01 - 가상 인프라 비용 국제비교(Example)", "md",
         dict(institution_raw=None, metadata_parse_status="partial",
              family_core="가상 인프라 비용 국제비교(Example)")),
        ("정책 - 2023-12-27 - 「국토부」 - 제4차 물류시설개발 종합계획(2023-2027)(요약)", "pdf",
         dict(variant_type="summary", family_core="제4차 물류시설개발 종합계획", authority_tier=1)),
        ("정책 - 2023-12-27 - 「국토부」 - 제4차 물류시설개발 종합계획(보도자료)", "pdf",
         dict(variant_type="press_release", family_core="제4차 물류시설개발 종합계획", authority_tier=2)),
        ("정책 - 2025-12-17 - 「복지부」 - 제5차 치매관리종합계획(2026-2030)(보도참고)", "pdf",
         dict(variant_type="press_release", family_core="제5차 치매관리종합계획", authority_tier=2)),
        ("정책 - 2026-01-19 - 「과기부」 - 가상 설명회 자료 - 4 - 가상 제도 개선(요약)", "md",
         dict(variant_type="summary", family_core="가상 설명회 자료 - 4 - 가상 제도 개선")),
        ("정책 - 2026-06-26 - 「국과심」 - 제6차 과학기술기본계획(2026-2030) - 세부 실천과제 내역", "xlsx",
         dict(variant_type="excerpt", family_core="제6차 과학기술기본계획")),
        ("정책 - 2026-06-26 - 「국과심」 - 제6차 과학기술기본계획(2026-2030)", "pdf",
         dict(variant_type="original", family_core="제6차 과학기술기본계획")),
        ("논문 - 2026-02 - 「가상대」 - 가상 질의응답 시스템 설계(홍길동)(요약)", "html",
         dict(variant_type="summary", family_core="가상 질의응답 시스템 설계(홍길동)")),
    ]
    for stem, ext, expect in cases:
        got = parse_filename(stem, ext)
        for k, v in expect.items():
            assert got[k] == v, f"{stem}\n  field {k}: expected {v!r}, got {got[k]!r}"
    # family key pairs that must match
    a = parse_filename("정책 - 2023-12-27 - 「국토부」 - 제4차 물류시설개발 종합계획(2023-2027)(전문)", "pdf")
    b = parse_filename("정책 - 2023-12-27 - 「국토부」 - 제4차 물류시설개발 종합계획(고시문)", "pdf")
    assert family_key(a) == family_key(b)
    print("filename_meta self-test passed")
