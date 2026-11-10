# 데이터 스키마 계약 (v0.1, Phase 0 확정)

교차 검토(2026-08-09)를 거친 필수 계약. 구현은 이 계약을 따르고, 변경 시 이 문서를 먼저 갱신한다.

## 1. file (물리 파일 단위)

| 필드 | 타입 | 설명 |
|---|---|---|
| file_id | str | 파일 고유 ID (`f_` + sha256 앞 12자) |
| source_root_label | str | 소속 코퍼스 루트 (`corpus` / `mhk`) — §12 |
| source_path | str | **해당 루트** 기준 상대 경로 (NFC 정규화) |
| source_folder | str | 루트 내 최상위 폴더명 (예: "policies") |
| ext | str | 소문자 확장자 |
| size_bytes | int | 크기 |
| content_sha256 | str | 원본 해시 (무결성·중복·idempotency 기준) |
| mtime | str | 원본 수정시각 ISO 8601 |

## 2. document / family (논리 문서 단위)

| 필드 | 타입 | 설명 |
|---|---|---|
| document_id | str | 개별 문서·판본 ID |
| family_id | str | 동일 정책·보고서 계열 ID (파일명 기반 1차 추정 + 사람 검토 1회) |
| variant_type | enum | original / full_text / summary / press_release / notice / excerpt |
| authority_tier | int | 1=원문·고시, 2=공식 보도자료, 3=운영자 작성 요약 등 파생 |
| evidence_eligible | bool | 근거 인용 허용 여부 (tier 3 요약은 false — context_only) |
| relation_type | enum | derived_from / summarizes / accompanies / revises |
| relation_confidence | float | 관계 추정 신뢰도 |
| title | str | 제목 (파일명 파싱) |
| institution_raw / institution_normalized | str | 기관명 원문 / 통제 어휘 정규화 |
| filename_date | str | 파일명의 날짜 (의미 미확정 — 발간일로 자동 승격 금지) |
| publication_date | str? | 발간일 (확인된 경우만) |
| date_semantics | enum | published / effective / filename_only / unknown |
| metadata_parse_status | enum | ok / partial / failed (실패는 오류가 아니라 명시적 unknown) |

## 3. page (PDF 전용)

| 필드 | 타입 | 설명 |
|---|---|---|
| pdf_page_index | int | 내부 0-based 인덱스 |
| pdf_page_number | int | 파일 첫 장=1 표시 번호 |
| printed_page_number | int? | 본문에 인쇄된 면 번호. 추출 불가 시 null (offset 추정 금지) |
| printed_page_confidence | float? | 인쇄면 번호 신뢰도 |
| page_status | enum | ok / empty / low_text / suspect_encoding / suspect_reading_order / image_only / extract_error / manual_override (empty=텍스트·이미지 모두 없는 간지, 오류 아님) |
| quality_signals | obj | 문자수·한글완성형비율·자모비율·U+FFFD비율·PUA비율 등 (의심 선별용, 단일 임계 자동실패 금지) |

인용 표기: 기본 "PDF p.17". printed_page_confidence 높을 때만 "인쇄면 12 (PDF p.17)".

## 4. chunk

| 필드 | 타입 | 설명 |
|---|---|---|
| chunk_id | str | 청크 ID |
| document_id | str | 소속 문서 |
| locator | obj | typed locator (아래 5) |
| text_raw | str | 인용용 원문 (NFC만 적용, 변경 최소화) |
| text_normalized | str | 검색·임베딩용 (공백 정리, 필요 시 NFKC — 검색용에만) |
| token_count | int | BGE-M3 tokenizer 기준 (목표 350~700, 중첩 50~80) |
| page_span | list | 걸친 모든 페이지 |
| section_path | str? | 장절 경로 (추출 가능 시) |

## 5. typed locator (비PDF에 페이지 번호 부여 금지)

| 파일 유형 | locator (구현 정합 v2) |
|---|---|
| PDF | {type:"pdf_page", page_start, page_end} (0-based pdf_page_index 범위) |
| MD | {type:"md", heading_path, line_start, line_end} |
| HTML(→MD 변환) | MD와 동일 (converted_from:"html") |
| XLSX(→MD 변환) | {type:"sheet_range", sheet, converted_from:"xlsx"} (행 범위는 차기 확장) |

## 6. license / rights

| 필드 | 값 (Phase 0 기본) |
|---|---|
| servable_scope | local_dev_only (hard gate — remote 서빙 전 재판정 필수) |
| rights_review_status | deferred |
| license_label / license_url / source_url / publisher / accessed_at / attribution_text / commercial_use_status / redistribution_status | 스키마만 마련, 상용 수집 단계에 기록 |

## 7. MCP 응답 계약 v2 (2026-08-09 3라운드 적대 검토로 개정)

이 계약은 임시 안전 조치이며 상품 약속의 완성이 아니다. 서버측 보수적 support 판정기는 golden set 확보 후 상용 단계 과제.

| 필드 | 값 | 의미 |
|---|---|---|
| response_schema_version | "v2" | 계약 버전 |
| retrieval_status | candidates_returned / no_candidates | 기계적 invariant("성공한 검색에서 results 비어있지 않음")만 표현. 관련성·품질 신호 아님. 인덱스 누락·모델 실패는 no_candidates가 아니라 명시적 tool error |
| support_status | "not_assessed" 고정 | 서버는 직접 뒷받침 여부를 판정하지 않음(미래 판정기 부착 지점) |
| direct_answer_check_required | true 고정 | 클라이언트가 직접지지 검증 수행 |
| corpus_absence_claim_allowed | false 고정 | 코퍼스 전체 부재 선언 금지. 허용 결론은 "반환된 후보에서는 질문을 직접 뒷받침하는 근거를 확인하지 못했습니다"뿐. **대칭 규칙**: 코퍼스 밖 문서의 존재 단정도 금지 — passage의 향후 일정·추진계획은 예정이지 산출물 생성의 근거가 아님 (2026-08-10 실사례: 계획표의 "5월 추진계획 확정·7월 설명회"에서 문서 존재를 추론해 사용자에게 잘못된 입수 지시를 냄. 해당 문서들은 실제로 생성되지 않음) |
| results[] | 인용 허용 청크만 | citation_allowed=true만. eligibility 필터는 각 검색 leg의 후보 컷오프 이전 적용. raw 점수(cosine·rrf·rank)는 응답에 미포함(내부 로그·개발 CLI 전용) |
| results[].excerpt_truncated | bool | 발췌 절단 여부 명시 |
| detected_extraction_gaps[] | {doc_id, title, gap_type, reason} | 휴리스틱 탐지된 추출 갭. 빈 배열이 코퍼스 완전을 뜻하지 않음. gap은 support_status를 변경하지 않음 |
| truncated / omitted_chunk_ids | bool / [] | 토큰 예산으로 누락된 결과 명시 |

citation_allowed 정의: "현 코퍼스 정책상 인용 허용"(현재는 authority_tier 1·2에서 파생). "공식 출처" 또는 "직접지지 판정"과 등치 금지. 파생 요약(tier 3)은 검색 응답에서 기본 제외 — 공식 결과와 같은 family인 경우 get_document_metadata로 존재 확인 가능하며, 비허용-only family는 검색에서 의도적으로 발견 불가.

intent 입력: 로그 전용(검색 미사용). golden set에서 question-only 대비 실험 후에만 활용 재검토.

snapshot 정합성: lexical 인덱스와 dense 인덱스의 chunk_id 지문(sha256)이 일치해야 서버 기동(불일치 시 fail-closed). index_snapshot_id는 query log에 기록.

### 7-1. R1b 추가 개정 (2026-09, UPDATE-PLAN-R1 §3·§11·§12) — v2 유지, 필드 추가만

`response_schema_version`은 "v2"를 유지한다. 기존 필드의 이름·타입·값은 바뀌지 않으며(검색 결과 자체도 불변 — `scripts/test_r1b_invariance.py`가 변경 전 기준선과 비트 단위로 대조), 아래 필드가 **추가**된다. 알 수 없는 키를 거부하는 엄격한 클라이언트(`additionalProperties: false`)는 정의상 새 응답을 거부하므로, 이는 공개 알파의 **고지된 추가 변경**이다(README 변경 이력).

| 필드 | 도구 | 값 | 의미 |
|---|---|---|---|
| response_contract_revision | 전체 | "2026-09-r1b" | 추가 개정 식별자 |
| retrieval_semantics | search | "nearest_candidates" 고정 | 결과는 가장 가까운 후보이지 직접지지 판정이 아님(C4). 점수 임계값 도입 금지 |
| answer_requirements | search·passages | 불리언 사전 7항 | 답변 작성 요건(C1). 자연어 문장은 이 사전에서 **단일 함수**(`requirements_text`)로 파생되어 서버 instructions에만 실린다 — 응답에 문장을 반복하지 않는 이유는 크기 예산(아래) |
| answer_requirements_note | search·passages | 한 줄 | 규칙 원문 위치와 핵심 3항(인용 문서 URL만·URL 미확보 표기·인접 수치 대체 금지) 상기 |
| candidate_source_manifest[] | search·passages | {doc_id, title, source_url\|null, source_url_status: registered\|unavailable} | 직렬화된 results(예산 생략분 제외)의 문서를 최초 등장 순으로 중복 제거한 **후보 목록**(C2). 답변 하단 출처는 이 중 실제 인용한 문서만. null은 "원문 URL 미확보"로 표기하고 추정·웹 대체 금지 |
| locator_semantics | search·passages | {pdf_page_index_base: 0, cite_with: "citation_label"} | locator의 page_start/page_end는 0-based 내부 색인(C5). 사람용 쪽수 표기는 citation_label만 |
| citation.variant_label / authority_label | search·passages | 자연어 | 열거형 원값의 자연어 라벨(C9). 폐쇄 매핑, 미지 값은 "상태 미확인", null은 "등재 정보 없음"(null ≠ 비계획·비법정) |
| citation.plan.status_label / statutory_label | search·passages | 자연어 | 예: current → "현행 계획", superseded → "후속 계획으로 대체된 구판" |
| labels{} | get_document_metadata | 4개 라벨 | 위 라벨의 문서 메타데이터 판 |

instructions(연결 지침)는 `build_instructions(ANSWER_REQUIREMENTS)`로 생성된다(1,079자 / BGE 524토큰, 회귀 방지 상한 1,200자·600토큰). C10(유사 수치 대체 금지)과 부재 표현 규칙: 허용 문구는 "반환된 후보에서는 요청하신 <문서>의 <연도>·<지표> 값을 직접 확인하지 못했습니다"이며 "코퍼스에 없다" 단정은 금지(§7 corpus_absence_claim_allowed=false와 정합). 인접 수치는 참고 정보임과 문서·연도 귀속을 밝힐 때만 언급.

**크기 예산(§9)의 실측 보정**: §9의 4,000/8,000은 발췌·passage 텍스트 기준이었고 JSON 오버헤드는 미계상이었다. R1b에서 전체 직렬화 응답의 BGE 토큰을 게이트(`scripts/test_r1b_size.py`)로 측정한다 — 변경 전 최악치(k=8·current) 6,923, 규칙 문장을 응답마다 실은 초안 8,327(초과), manifest 축소·문장→한 줄 안내 후 **7,811**(≤8,000). 런타임 절단은 추가하지 않는다(결과 불변 원칙). 상한은 발췌 ≤450토큰(전 청크 실측 최대 450·첫 줄 초과 0)이라는 코퍼스 성질에 의존하므로 코퍼스 갱신(R-C)마다 재실행한다.

## 8. ingest run (idempotency·재현성)

| 필드 | 설명 |
|---|---|
| run_id | 인제스트 실행 ID |
| extractor_name / extractor_version | 추출기와 버전 |
| normalization_version | 정규화 규칙 버전 |
| index_snapshot_id | 색인 snapshot ID + 참조 원본 해시 목록 |

원칙: 동일 content_sha256 재처리 생략. 무음 누락 0건 — 모든 파일·페이지에 상태 부여. snapshot 원자 교체, 이전 세대 보존.

## 9. MCP 응답 예산

- search_rnd_facts: 발췌 250~450토큰 × 기본 5건(최대 8). 기본 ≤4,000토큰, hard cap ≤8,000토큰. 초과 시 truncated=true + 누락 passage ID + continuation
- get_passages: passage ID 기반, max_chars 제한. 전체 문서 본문 반환 금지

## 10. 후보 창 확장 (2026-08-13 도입)

문제: 긴 문서 하나가 검색 leg의 후보 창을 채우면, 융합 이후 적용되는 family cap이 잘라낸 자리를 채울 후보가 남지 않아 요청 수보다 적게 반환된다. 실측(2026-08-13): "건설 분야 기술수준" 질의에서 단일 문서가 BM25 후보 50개 중 47개(94%)를 점유해 k=8 요청에 4개만 반환. depth 100에서는 family cap 적용 후 확보 가능 수가 9개.

규칙:
- 후보 창은 `CANDIDATES`(50)에서 시작한다. family cap 적용 후 k가 채워지면 즉시 종료 — 기존 동작과 동일하다
- 부족할 때만 창을 ×`EXPANSION_FACTOR`(3)로 넓힌다. 상한은 `MAX_CANDIDATES`(2000)
- **RRF는 항상 raw rank를 쓴다.** 창을 넓히는 것은 같은 순위의 더 긴 prefix를 취하는 것이므로 순위가 재번호되지 않는다. 필터 후 0부터 재번호하면 깊은 후보가 인위 승격된다(raw 48위 1/108 → 5위 1/65)
- dense leg는 질의 임베딩과 점수 계산을 라운드마다 반복하지 않는다(1회 계산 후 slice)
- 종료 규칙: **"k 요청이면 항상 k 반환"이 아니라 "활성 leg를 소진했을 때 family cap 아래 k개가 존재하면 k개 반환"**이다. cap을 깨서 k를 채우지 않는다
- leg 내부 family 상한은 도입하지 않는다 — 기술수준평가 14권처럼 서로 다른 내용이 한 family로 묶인 사례가 있어, leg 단계 family 제한은 관련 문서를 조기 탈락시킨다

~~미해결(별건): 기술수준평가 family 재분류~~ → §11과 함께 해결 (2026-08-13). " - 세부 - NN 분야명" 정확 패턴의 세부권은 별개 family로 분리된다(변형이 아니라 별개 내용). 인제스트 QA 리포트가 패턴 적중 목록을 출력하므로 신규 코퍼스 편입 시 오적중 여부를 사람이 확인한다. 예외는 FAMILY_OVERRIDES로 재결합.

## 11. 계획 메타데이터 레지스트리 (2026-08-13 도입)

목적: "이 계획이 현행인가"와 "법정계획인가"는 자동 추출이 불가능한 사람 확인 값이다. 큐레이션 산출물을 재생성되는 파일(documents.jsonl)에 두면 유실되므로 별도 레지스트리로 관리한다.

- 위치: `curation/plans.jsonl` — **git 추적** (코퍼스 원문과 달리 큐레이션 산출물이므로)
- 병합: 인제스트가 레지스트리를 읽어 documents.jsonl에 병합한다. 캐시된 문서도 파일명 메타·레지스트리 병합·family는 매 실행 재계산된다(추출 결과만 캐시)

| 필드 | 값 | 의미 |
|---|---|---|
| plan_id | P001… | 레지스트리 기본키. 계획의 공식 판본마다 하나, 재생성하지 않는 안정 ID |
| plan_title | str | 큐레이션 제목 (파일명 파싱 title을 덮어쓰지 않음) |
| plan_status | current / superseded / expired / unknown | boolean 금지 — false가 대체·기간종료·미확인 중 무엇인지 구분해야 함 |
| plan_status_as_of | YYYY-MM-DD | 현행성 확인 기준일. current에는 필수 (현행성은 시간에 따라 변함) |
| current_basis | str | 현행성 판단 근거 (짧게 — 긴 원문 발췌 금지) |
| statutory_status | statutory / non_statutory / unknown | 법정계획 여부 |
| statutory_basis | str? | 근거 법령명 |
| plan_ministry / announced_on / plan_period / rnd_level / rnd_quote / source_url | — | 조사 확보값 보존 |
| bindings | [{filename_stem, content_sha256?}] | 문서 연결. sha 우선 매칭(내용 교체 시 자동 승계 차단이 목적), 미편입 문서는 stem만 두고 편입 시 sha를 채움 |

규칙:
- **계획기간만으로 현행성을 자동 계산하지 않는다** — 기간에 현재 연도가 포함돼도 대체됐을 수 있고, 기간이 끝나도 후속 확정 전까지 적용될 수 있음
- 레지스트리 값은 FTS 본문으로 색인하지 않는다 (사람이 쓴 메타 문구가 정부 원문 passage처럼 검색되면 안 됨)
- chunks 테이블에는 `plan_id`, `plan_status`만 복제 (필터용). 서술형 필드는 문서 메타데이터에서 조회
- 비계획·미큐레이션 문서는 null. **null ≠ "비계획으로 확인됨"** — "계획 메타데이터 없음, 현행성 확인 불가"를 뜻한다. unknown은 "계획임은 확인됐으나 해당 속성 미확인"에만 사용

검색 필터 `plan_status_filter` (기본 "all"):
- "all": 기존과 동일 — 비계획·미큐레이션·구판 모두 검색
- "current": plan_status='current'로 확인된 계획 문서만. null·unknown 제외
- 필터는 양쪽 검색 leg의 후보 컷오프 이전에 적용 (§10과 동일 원리 — 융합 후 제거하면 제외 대상이 후보 창을 점유)
- **기본 구판 제외 금지** — "2023년 당시 기준" 같은 역사 질의를 손상시킴. 질문에 "현재"가 있다고 서버가 자동 선택하지 않는다(호출자 명시 선택). v2 계약과의 정합: plan_status는 사람이 검토한 문서 속성이고 필터는 검색 범위 제한일 뿐, passage의 직접지지 판정이 아니다

## 12. 코퍼스 소스 루트 (2026-08-14 도입)

목적: 코퍼스가 mhk 단일 폴더에서 벗어나 배치 단위로 증가한다. 원본이 어디에 있어야 하는지를 계약으로 고정한다. 교차 검토(2026-08-14) 결과 채택.

선언 위치: `src/rnd_factlink_mcp/corpus_roots.py` — manifest 빌드와 인제스트가 **같은 선언 하나**를 쓴다 (루트 정의 이중화 금지).

| label | 경로 | 성격 |
|---|---|---|
| corpus | `~/rnd-factlink-corpus` | 프로젝트 정본 원본. 리포 밖 (vault/를 지우고 재빌드해도 원본이 남아야 함), mhk 밖 (프로젝트가 mhk에 쓰지 않음). **백업 대상은 이 디렉터리** |
| mhk | 리포 밖 로컬 설정(`corpus_roots.LOCAL_ROOTS_PATH`) | 두 번째 원본 루트. strictly read-only — 쓰기·이동·개명·삭제 금지. 위치·폴더 목록은 공개 리포에 두지 않음 |

규칙:
- `vault/`에 원본을 두지 않는다. `vault/raw/<sha>.<ext>`는 특정 인제스트 시점의 content-addressed 스냅샷일 뿐 **백업이 아니다** (같은 디스크·git 미추적). raw만으로는 원래 파일명·폴더·루트가 소실되어 manifest를 재생성할 수 없다
- **primary 선택은 루트 우선순위로 정한다** (`corpus` 0 < `mhk` 1). 같은 sha가 여러 루트에 있을 때 제목·기관·variant·family·인용 허용성이 전부 primary의 **파일명**에서 파생되므로, 경로 문자열 정렬이 이를 결정해서는 안 된다
- 파일명은 경계에서 NFC로 정규화한다 (macOS는 한글을 NFD로 저장, 레지스트리·비교 키는 NFC). NFC 변환 후 같은 루트에서 경로가 충돌하면 Gate 0 FATAL
- 라벨 없는 구 manifest 레코드는 `mhk`로 해석 (다중 루트 도입 이전 레코드는 전부 mhk 출신)

### Gate 0 (corpus baseline)

- 코퍼스 빌드(build_manifest·인제스트)는 운영자의 비공개 큐레이션 파일(Gate 0 승인 대장, 비공개 제외 목록, 두 번째 루트 선언)이 있어야 실행된다. 이 파일들과 원본 문서는 공개 리포에 없으므로, 공개 클론에서는 코퍼스를 다시 만들 수 없다.
- 승인 기준: `~/rnd-factlink-corpus/curation/corpus_baseline.json` (리포 밖 비공개 보관 — 운영자의 비서빙 보유 문서까지 전부 나열하므로 공개 리포에 두지 않음. 원본과 함께 백업). 경로 상수 `corpus_roots.PRIVATE_CURATION`. 루트별 `상대경로 → {sha256, ext, size_bytes}`. mtime은 복사·복원으로 흔들리므로 제외
- 총계가 아니라 **파일별 sha256**을 비교한다. 총계만 보면 1건 삭제 + 1건 추가, 같은 크기의 내용 교체가 통과한다
- manifest는 게이트 통과 후에만 원자적으로 교체한다. 실패한 실행이 잘못된 manifest를 남겨 후속 인제스트가 소비하는 일이 없어야 함
- 코퍼스 증가는 정상 작업이므로 코드를 고치지 않는다: 신규 파일을 루트에 두고 `scripts/build_manifest.py`로 변경 목록(ADDED/REMOVED/CHANGED)을 검토한 뒤 `--approve-baseline`으로 승인한다. 승인은 데이터 변경이며 코드 변경이 아니다

### 코퍼스 제외 (corpus_exclusions.jsonl, 2026-08-14 도입)

편입 대상에서 뺀 문서를 **내용 해시로** 기록한다(git 추적). 파일명을 바꿔도 우회되지 않는다. 코퍼스 루트에서 이 해시가 발견되면 `build_manifest.py`가 Gate 0 FATAL로 즉시 중단하며, `--approve-baseline`으로도 덮어쓸 수 없다.

- 현재 등록: 국가연구개발혁신법 매뉴얼 본권·부록·별권 1~4 6건 — 기존 korean-rnd-regs-mcp의 `search_manual` 담당 범위 (운영자 결정 2026-08-14)
- 해제: 해당 줄을 지우면 된다
- 참고: 색인만 제외하는 skip 모드를 2026-08-14 도입했다가 같은 날 롤백함(군더더기 판단). 색인에서 빼기로 한 문서는 루트에서 파일을 제거하는 것이 정본 절차다

### 재빌드 순서 (불변)

인제스트 → lexical → dense → 서버 재시작. lexical만 갱신하면 chunk 스냅샷 지문이 dense와 어긋나 서버가 fail-closed 한다(§7). 서버는 documents·dense·eligibility mask를 프로세스 메모리에 캐시하므로 재시작이 필요하다.
