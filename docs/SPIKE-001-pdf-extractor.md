# SPIKE-001: PDF 추출기 확정 (pdfplumber vs pypdfium2)

- 일자: 2026-08-09 / 대상: 대표 PDF 12건 (전체 61건 중) / 도구 버전: pdfplumber 0.11.10, pypdfium2 5.12.1
- 원데이터: `vault/derived/spike/spike_summary.json`, 페이지별 발췌 `vault/derived/spike/<short>.md` (git 미추적)

## 결론

**pdfplumber를 기본 추출기로 확정.** pypdfium2는 이미지 전용 페이지 탐지·QA 렌더링·교차검증용으로 병용.

근거 (사전 확정된 기준 적용 — 속도는 기준 아님):
1. 텍스트 품질: 12건 중 11건에서 두 추출기 동등 (empty 페이지 목록 일치, 깨짐 신호 동일, 총 문자수 차이 약 3%는 공백 처리 차이)
2. 실패 양식: 깨진 CMap 문서(tech_level)에서 pdfplumber는 `(cid:NNNN)` 명시 실패, pypdfium2는 그럴듯한 유니코드 쓰레기 문자 출력. 근거 인용 상품에서 탐지 가능한 명시 실패가 안전
3. 도구 지원: pdfplumber는 문자·단어 좌표, 표 추출, 시각 디버깅 제공 — 머리말 분리·인쇄면 번호 추출(페이지 상/하단 영역 텍스트)에 필요

## 문서별 결과 요약

| 문서 | 페이지 | 상태 |
|---|---|---|
| invest_2027 (투자방향) | 147 | 정상. 읽기 순서 양호, empty 8 (간지) |
| basic_plan6 (기본계획) | 143 | 정상 |
| briefing_budget (발표자료) | 20 | **사실상 이미지 전용** (총 274자 — 슬라이드가 이미지) |
| invest_reco (투자 권고) | 69 | 정상 |
| budget_guideline (편성지침) | 64 | 정상 |
| logistics_notice (고시문) | 3 | 정상 (스캔 아님) |
| logistics_plan5 (물류기본계획) | 170 | 정상. PUA 최대 4.4% 페이지 존재 (특수기호 수준) |
| agenda123 (국정과제) | 195 | 정상. empty 37 (표지·간지 다수) |
| tech_level (기술수준평가) | 207 | **양쪽 모두 깨짐** — pl은 194쪽 (cid:) 오염, pf는 140쪽 쓰레기 문자. ToUnicode CMap 결함 문서 |
| long_doc (장문 PDF) | 355 | 정상 |
| pua_font (PUA 폰트 PDF) | 79 | 본문 일부 4쪽 PUA 폰트 깨짐 (양쪽 동일) |
| two_column (영문 2단) | 6 | 정상 |

## Phase 1 반영 사항

1. tech_level 계열 격리: 본체 PDF는 `suspect_encoding` 격리. 동일 폴더의 세부 분야별 PDF 11건도 같은 결함 가능성 — 전체 인제스트 시 페이지 품질 신호로 검사
2. 이미지 전용 탐지 보강: "빈 페이지 + 이미지 존재"만으로 부족 — briefing_budget은 페이지당 수 자(쪽번호 등)가 있어 empty 아님. **low_text(<30자) + 이미지 면적** 조합으로 판정. 부처합동설명회 발표자료 계열(복수 파일)이 동일 유형일 가능성 높음 — `corpus_or_extraction_gap` 처리, OCR Phase 1.5 후보
3. quality_signals에 추가: `cid_count`("(cid:" 출현), `garbage_suspect`(비ASCII 다량 + 한글완성형 비율 < 5%) — 스파이크에서 실측 검증된 신호
4. 간지·표지의 empty 페이지는 정상 (invest_2027 8쪽, agenda123 37쪽) — empty 자체를 오류로 취급하지 않음 (계획의 "단일 임계 자동실패 금지" 확인)

## 미결

- tech_level 격리 문서의 대체 경로 (OCR 또는 KISTEP 원공개본 재수집)는 golden set 질문이 해당 문서를 요구할 때 결정
