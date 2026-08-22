# 연금 상담 AI 에이전트

> 제10회 2026 미래에셋증권 AI Festival 출품작 — 연금 Agent
> CLOVA Studio(HyperCLOVA X) 기반 **결정적 슬롯 조회 + 하이브리드 RAG + Text-to-SQL** 연금 상담 에이전트

---

## 1. 개요

퇴직연금(DB·DC·IRP)과 개인연금(연금저축) 질의에 대해 **제도 규정(비정형 문서 + 정형 규칙)** 과
**상품 수치(정형 DB)** 를 동시에 근거로 삼아 답변하는 에이전트입니다.

질문의 성격을 먼저 분류한 뒤 필요한 검색 경로만 선택적으로 태워,
"제도가 어떻게 되어 있는가"와 "그래서 어떤 상품이 얼마인가"를 하나의 답변으로 통합합니다.

**제도(퇴직연금·연금저축 규정) 트랙과 상품(펀드 투자설명서) 트랙은 서로 다른 방식으로 조회합니다.**
제도 트랙은 팀원(Kwonjunil)이 구축한 결정적 슬롯 조회 + 3치 논리 엔진을 이식해 썼고,
상품 트랙은 기존 Text-to-SQL을 그대로 씁니다. 아래 2장에서 왜 이렇게 나눴는지 설명합니다.

### 설계 원칙

**단일턴 완결성** — 평가가 단일턴으로 진행되므로 되묻고 기다릴 수 없습니다.
조건이 부족하면 확인 질문을 **첫 답변 안에 포함하고 동시에 조건별 결론까지** 제시합니다.
(`Clarify` 대신 `Assume + Expose` 전략)

**근거 고정** — 금액·세율·한도·보수율은 검색된 원문의 값을 변형 없이 인용합니다.
근거에 없는 사실은 생성하지 않고, 답할 수 없으면 한계를 명시합니다.
근거가 질문의 주제를 다루더라도 질문이 묻는 **구체적인 조건의 조합**까지 근거 문단에
그대로 있는지 별도로 확인합니다 — 인접한 일반 규정을 근거로 특수한 조합을 단정하지 않습니다.

**LLM에게 산수를 시키지 않는다** — 연금수령한도(`평가액/(11-연차)×120%`) 같은 정해진 공식은
HCX가 계수를 잘못 적용해 값이 8배 틀리는 사례가 반복 보고됐습니다. 코드로 직접 계산해
"이 값을 그대로 쓰라"고 근거에 못박습니다(`pension_calc.py`).

**단정 추천 회피** — 상품은 조건별 후보와 판단 기준을 제시하며 하나를 단정하지 않습니다.

**전제 교정** — 질문에 사실과 다른 전제가 섞여 있으면 먼저 바로잡고 답합니다.
"…만 알려달라"는 요구가 있어도 불리한 조건과 유의사항은 반드시 포함합니다.

---

## 2. 아키텍처

```
                    GET /answer?question_id=&question=
                                 |
                    +------------------------+
                    |  PensionOrchestrator   |
                    +------------------------+
                                 |
                    (1) 의도 분류 (규칙 우선, 애매할 때만 HCX-007 Router)
                       SQL_FIN / SQL_FUND / HYBRID / RAG
                                 |
                +----------------+----------------+
                |                                 |
      [ 제도 트랙 — 이식 ]                [ 상품 트랙 — 기존 ]
                |                                 |
      +------------------+              +------------------+
      | InstitutionSQL   |              |  PensionSQLAgent |
      |  Agent           |              |   (Text-to-SQL)  |
      | (결정적 슬롯조회  |              +------------------+
      |  + 3치 조건판정) |                        |
      +------------------+              fund_prospectus_v2.sqlite
      | InstitutionRAG   |                  (펀드 100종)
      |  Agent           |
      | (BM25+벡터 RRF)  |
      +------------------+
                |
      pension_rules.db +
      junil_rag_embeddings_v3.json
      (팀원 Kwonjunil 제공)
                |                                 |
                +----------------+----------------+
                                 |
                    (2) 연금수령한도 등 정해진 공식은
                        코드로 직접 계산 (pension_calc.py)
                                 |
                    (3) HCX-007 통합 답변 생성 (synthesize_answer)
                                 |
        { question_id, question, retrieved_context,
          think_trace, answer }
```

### 왜 제도 트랙만 교체했는가

원래는 제도 쪽도 상품과 똑같이 Text-to-SQL + 벡터 유사도 하이브리드 서치를 썼습니다.
그런데 같은 대회를 준비하던 팀원(Kwonjunil)이 **제도 쪽만** 다른 방식으로 훨씬 깊게
파고든 결과물을 갖고 있었습니다.

- **결정적 슬롯 조회** — 매 질문마다 LLM이 SQL을 새로 생성하는 대신, 정규식으로 조건을
  추출해 슬롯을 조회합니다. SQL 생성 실패라는 실패 모드 자체가 없고 HCX 호출도 0회입니다.
- **3치(met/unmet/unknown) 조건 판정** — 요건을 "충족"/"불충족"으로만 보지 않고
  "판정 보류"를 별도로 다룹니다. `condition_role`로 요건이 무너지면 사실 자체를 버리는
  `selector`와, 무너져도 "요건 미충족"이라는 답 자체가 되는 `requirement`를 구분합니다.
- **BM25 + 벡터 RRF 융합 검색** — 벡터 유사도 단독 대신 키워드 검색(BM25)과 벡터 검색을
  Reciprocal Rank Fusion으로 합칩니다.
- **코퍼스 자체가 더 풍부합니다** — 팀원의 `junil_rag_embeddings_v3.json`(760청크)에는
  "디폴트옵션 FAQ 100선", 과학기술인공제회 안내 같은 문서가 포함돼 있어, 애초에
  기존 코퍼스에는 없던 질문(승인취소·회원가입 절차 등)까지 실제로 커버합니다.

반면 **펀드(투자설명서) 트랙은 팀원 구현에 아직 없어서** 기존 Text-to-SQL을 그대로
유지했습니다. 두 트랙의 결과는 `institution_format.py`가 기존 `synthesize_answer`
프롬프트가 이해하는 `[확정 수치]` / `[요건 판정 보류]` / `[계산 보류]` 라벨 규약으로
직렬화해 하나의 생성 단계로 합류시킵니다 — 이 라벨 규약은 팀원의 `orchestrator.compose()`
관례를 그대로 가져온 것입니다.

### 검색 전략: 메타데이터 가중 하이브리드 서치 (펀드 트랙)

펀드 쪽 검색(`rag_agent.py`)은 여전히 순수 벡터 유사도 + 메타데이터 가산점을 씁니다.

```
hybrid_score = cosine_similarity(query, chunk)
             + 0.08 x (연금 유형 일치)
             + 0.06 x (주제 일치)
             + 0.05 x (지식 속성 일치 개수)
             + 0.03 x (키워드 포함)
             - 0.08 x (질의에 유형 언급이 없는데 특수 유형 문서인 경우)
```

질의 태깅(`rag_agent.detect_query_metadata`)과 청크 태깅(`build_embeddings.tag`)은
**반드시 같은 어휘를 사용**합니다. 두 곳의 태그 표기가 어긋나면 가산점이 통째로 죽습니다.

관련성 임계값(`RAG_MIN_TOP_SCORE`) 메커니즘도 있지만 기본값은 **비활성(0)** 입니다.
`calibrate_threshold.py`로 실측해보니 코퍼스 내 질문 최저 점수(0.64)와 코퍼스 밖 질문
최고 점수(0.80)가 겹쳐서, 코사인 유사도 임계값 하나로는 정상 질문을 안 막고 환각만
깔끔하게 거를 수 없었습니다(`separated: false`). 그래서 임계값 대신 프롬프트 규칙
쪽에 더 투자했습니다 — 8장 참고.

---

## 3. 프로젝트 구조

```
.
├── main.py                     FastAPI 엔트리포인트 (공식 평가 스키마)
├── orchestrator.py             라우팅 -> 제도/상품 조회 -> 통합 생성
├── sql_agent.py                펀드 Text-to-SQL 생성·실행 (클래스 필터 안전장치 포함)
├── rag_agent.py                펀드 임베딩 · 하이브리드 검색 · 리랭킹 · 공용 HTTP 유틸
├── pension_calc.py             연금수령한도 코드 계산 (LLM 산수 회피)
│
├── institution_sql_agent.py    제도 결정적 슬롯 조회 + 3치 조건판정 (팀원 이식)
├── institution_rag_agent.py    제도 BM25+벡터 RRF 검색 (팀원 이식)
├── institution_format.py       위 두 결과를 synthesize_answer용 텍스트로 직렬화
├── tri.py                      3치 논리 (MET/UNMET/UNKNOWN) — 팀원 원본
├── safe_eval.py                수식 안전 계산 (AST 기반, eval() 미사용) — 팀원 원본
├── bm25.py                     BM25 랭킹 — 팀원 원본
├── budget.py                   검색 예산 관리 — 팀원 원본
│
├── parsers/                    원본 문서 파서 모듈 (펀드/제도 문서 공용)
│   ├── pdf_text.py             PDF 텍스트 추출 (pdftotext -> pdfplumber 자동 대체 + 캐시)
│   ├── docx_reader.py          docx 문단·표를 등장 순서 그대로 읽기
│   ├── legal_reader.py         안내문 말미 관련법령 조문 추출
│   ├── fund_fees.py            투자설명서 보수표 파서 (항등식 자기검증)
│   └── fund_sections.py        투자설명서 섹션 분할 + 운용실적 · 설정환매 추출
│
├── build_knowledge_base.py     (구) 제도 문서 -> knowledge_base_v2.json — 현재 미사용
├── build_fund_db.py            투자설명서 -> product_*_v2.json, fund_prospectus_v2.sqlite
├── build_sql_db.py             (구) 제도·세제 정형 DB -> financial_data.sqlite — 현재 미사용
├── build_embeddings.py         펀드 임베딩 인덱스 -> rag_embeddings_v3.json (CLOVA API 필요)
├── verify_data.py              재생성 결과 검증 리포트
│
├── evaluate.py                 evaluation_set.json(자체 10문항) 채점 러너
├── eval_answers.py             evalset_v1/v2.json(팀원 제공 40+26문항) 채점 러너
├── evalset_v1.json             팀원 제공 평가셋 v1 — 팀원 코퍼스 기준, 6라운드 튜닝됨(홈그라운드)
├── evalset_v2.json             팀원 제공 평가셋 v2 — "팀에서 받은 질문", 상대적으로 홀드아웃
├── evaluation_set.json         자체 평가 질의셋 (공식 참고질의 기반, 10문항)
├── smoke_test.py               서버 기동 후 규격·3경로 생존 확인용 스모크 테스트
│
├── check_hallu.py              (구) evalset_v2 coverage:none 4문항 환각 점검 — 아래 8장 참고
├── check_hallu2.py             (신) 지금 코퍼스 기준으로 재검증한 적대적 질문 6개
├── calibrate_threshold.py      RAG_MIN_TOP_SCORE 값을 코사인 분포 실측으로 정하는 도구
├── calibrate2.py                calibrate_threshold.py 2차 재측정
├── diag_hanwha.py               fund_prospectus_v2.sqlite 클래스 코드 체계 진단 도구
├── diag_q017.py                 /answer 응답의 실제 SQL·에러 원문을 직접 확인하는 도구
│
├── apply_fixes.py               2026-08-20 1차 개선 패치 (--revert 로 되돌리기 가능)
├── apply_fixes2.py              2026-08-20 2차 회귀수정 패치 (--revert 로 되돌리기 가능)
│
├── knowledge_base_v2.json      (구) 파싱된 제도 문서 지식베이스 — 현재 미사용
├── requirements.txt
└── .env.example
```

### 저장소에 포함되지 않은 항목

용량과 배포 제한 때문에 아래는 제외되어 있습니다(`.gitignore` 참고).

| 경로 | 내용 | 확보 방법 |
|---|---|---|
| `docs_renamed/` | 제도 문서 원본 58건 (pdf 37 · docx 18 · xlsx 2 · pptx 1) | 주최측 제공 |
| `투자설명서/` | 펀드 투자설명서 100건 | 주최측 제공 |
| `product_profiles_v2.json` | 펀드 프로필 파싱 결과 (14MB) | `python build_fund_db.py` |
| `product_sections_v2.json` | 펀드 섹션 청크 (27MB) | `python build_fund_db.py` |
| `data/fund_prospectus_v2.sqlite` | 펀드 정형 DB | `python build_fund_db.py` |
| `data/vector_db/rag_embeddings_v3.json` | (구) 제도 임베딩 인덱스, 현재 미사용 | `python build_embeddings.py` |
| **`data/pension_rules.db`** | **제도 정형 DB (226 facts · 125 conditions · 3 formulas)** | **팀원(Kwonjunil) 제공 — 직접 전달받아야 함** |
| **`data/vector_db/junil_rag_embeddings_v3.json`** | **제도 임베딩 인덱스 (760청크, 1024차원)** | **팀원(Kwonjunil) 제공 — 직접 전달받아야 함** |
| `.cache_pdftext/`, `.cache_embed.json` | 재실행 가속용 캐시 | 자동 생성 |
| `eval_reports/` | `evaluate.py` 실행 리포트 | 자동 생성 |
| `result_*.json`, `*_console.txt`, `hallu*.json`, `diag_*.json`, `calibrate_result.json` | 평가/진단 실행 산출물 | 각 스크립트 재실행 |

> **`data/pension_rules.db`와 `junil_rag_embeddings_v3.json`은 빌드 스크립트로 재생성할 수 없습니다.**
> 팀원 저장소(Kwonjunil/mirae_asset_competiton)에서 Git LFS로 관리되는 실제 데이터라, 이 저장소만
> 클론해서는 제도 트랙이 동작하지 않습니다. 팀원에게 두 파일을 직접 받아
> `data/pension_rules.db`, `data/vector_db/junil_rag_embeddings_v3.json` 경로에 두세요.

---

## 4. 실행 방법

### 4.1 환경 설정

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 4.2 API 키 등록

```bash
cp .env.example .env
```

`.env` 파일에 CLOVA Studio API 키를 입력합니다.

```
CLOVASTUDIO_API_KEY=nv-xxxxxxxxxxxxxxxx
```

> `.env` 는 `.gitignore` 에 등록되어 있습니다. 키를 커밋하지 마세요.

### 4.3 데이터 구축

**펀드 트랙**은 원본 폴더(`투자설명서/`)를 프로젝트 루트에 놓고 아래 순서로 실행합니다.

```bash
python build_fund_db.py           # 펀드 DB               (최초 20~25분, 이후 1~2분)
python verify_data.py             # 검증 리포트            (수 초)
python build_embeddings.py        # 펀드 임베딩 인덱스      (CLOVA API 필요)
```

**`build_fund_db.py` 소요 시간** — `pdftotext`(poppler)가 설치돼 있으면 30초,
없으면 `pdfplumber` 로 100건을 추출하느라 20~25분 걸립니다.
추출 결과는 `.cache_pdftext/` 에 캐시되므로 두 번째 실행부터는 1~2분입니다.

**`build_embeddings.py` 는 마지막에 한 번만** — 기존 인덱스에 같은 본문이 있으면
벡터를 재사용하므로 실제 API 호출은 신규 레코드 수만큼만 발생합니다.

**제도 트랙**은 빌드 스크립트가 없습니다 — 위 3장 안내대로 팀원에게 받은
`pension_rules.db`와 `junil_rag_embeddings_v3.json`을 `data/`, `data/vector_db/`에
그대로 두면 됩니다. (`build_knowledge_base.py`, `build_sql_db.py`는 제도 트랙을 Text-to-SQL로
직접 조회하던 이전 방식의 산출물이라 지금은 쓰이지 않습니다.)

### 4.4 서버 실행

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4.5 호출 예시

```bash
curl -G "http://localhost:8000/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=연금저축이랑 IRP 다 합쳐서 세액공제 얼마까지 되나요?"
```

```json
{
  "question_id": "Q-001",
  "question": "연금저축이랑 IRP 다 합쳐서 세액공제 얼마까지 되나요?",
  "retrieved_context": "...",
  "think_trace": "1. 의도 분류 및 라우팅: SQL_FIN -> 2. 제도 정형 DB 조회 완료 [FIN] (결정적 슬롯 조회, HCX 호출 0회) -> ...",
  "answer": "..."
}
```

---

## 5. 평가 및 검증 도구

빠른 생존 확인:

```bash
python smoke_test.py --n 5
```

자체 평가셋(10문항, 대회 공식 참고질의 기반) 채점 — 형식·근거표시·정확성·환각방지·
정보한계·안전성·라우팅을 종합 체크합니다.

```bash
python evaluate.py                # 전체
python evaluate.py --id OF-002    # 특정 문항만
```

팀원 제공 평가셋(v1 40문항 / v2 26문항) 채점 — 문자열 매칭 기반 1차 채점입니다.

```bash
python eval_answers.py --endpoint http://127.0.0.1:8000/answer \
    --evalset evalset_v1.json --out result_v1.json --show
```

환각 점검(적대적 질문 6개, 지금 코퍼스 기준으로 실제 무근거임을 직접 확인한 문항):

```bash
python check_hallu2.py
```

> `check_hallu.py`(구버전)는 `evalset_v2.json`의 `coverage:none` 4문항을 썼는데, 오늘 제도측
> 코퍼스를 팀원 데이터로 교체하면서 그중 3문항(V-10·V-21·V-26)이 실제로는 코퍼스 안에
> 답이 있는 것으로 확인됐습니다(`doc29.xlsx` "디폴트옵션 FAQ 100선", `doc27.pdf` 과학기술인공제회
> 안내). 즉 "환각"이 아니라 "정상적으로 근거를 찾아 답한 것"이라 이 4문항은 더 이상 유효한
> 환각 테스트가 아닙니다. `check_hallu2.py`는 지금 코퍼스(760청크) 전체를 텍스트 검색해서
> 실제로 다루지 않는 것을 확인한 새 질문 6개(그중 2개는 "실제 있는 주제 + 없는 디테일"
> 조합형)를 씁니다.

### 현재 결과 (2026-08-22 기준, 참고용)

| | v1 (40문항) | v2 (26문항) | 환각 점검 (check_hallu2, 6문항) |
|---|---|---|---|
| 정답 포함률 | 72.5% (29/40) | 81.8% (18/22) | 6/6 (근거 없이 단정한 문항 0건) |

**이 숫자를 그대로 실력 지표로 보면 안 됩니다.** 아래 사정이 있습니다.

- `evalset_v1.json`은 팀원이 자기 코퍼스로 만들어 6라운드 튜닝한 홈그라운드 셋이라,
  액면 비교는 불공정합니다. 상대적으로 공정한 비교 지점은 v2입니다.
- **같은 코드로 재실행해도 점수가 흔들립니다.** `temperature=0.1`이어도 LLM 생성 표현이
  매번 조금씩 바뀌고, 채점기는 정확한 문자열만 보기 때문에 몇 문항은 매 실행 뒤집힙니다.
  단발 점수보다 **결정적 버그(크래시·라우팅 비결정성)가 고쳐졌는지**를 우선 보십시오.
- 몇몇 "오답"은 실제로는 시스템이 평가셋의 정답 키보다 더 정확한 값을 찾은 경우입니다
  (예: Q-014 — 평가 키는 NH-Amundi 0.15%인데 시스템은 DB에서 더 싼 유진챔피언 0.1423%를
  찾아 정답 처리를 못 받음). 평가 키 자체가 데이터 재구축 이후 낡았을 가능성이 있습니다.

---

## 6. 데이터 현황

| 구분 | 규모 |
|---|---|
| 제도 문서 원본 (팀원 코퍼스) | 58건 |
| 제도 임베딩 청크 | **760건** (1024차원, 팀원 제공) |
| 제도 정형 사실(facts) | 226건 · 조건(conditions) 125건 · 공식(formulas) 3건 |
| 펀드 투자설명서 | 100건 (고유 92건) |
| 펀드 마스터 | 100종 |
| 클래스별 보수 | **1,053행** |
| 운용실적(수익률) | **6,192행** (83종) |
| 설정·환매 잔고 | **150행** (50종) |
| 펀드 섹션 청크 | 5,328건 |

### 정형 DB 스키마

**`data/pension_rules.db`** — 제도 (팀원 제공, 결정적 슬롯 조회 대상)

| 테이블 | 내용 |
|---|---|
| `pension_facts` | 제도 사실(226건) — 슬롯 조회의 대상 |
| `fact_conditions` | 사실별 성립 조건(125건) — 3치(met/unmet/unknown) 판정 대상 |
| `pension_formulas` | 계산 공식(3건) — `safe_eval`로 AST 기반 안전 계산 |
| `pension_type_codes` | 연금 유형 코드(10종) |
| `condition_keys` | 조건 키(27종) |

**`data/fund_prospectus_v2.sqlite`** — 펀드

| 테이블 | 내용 |
|---|---|
| `fund_products` | 펀드 마스터 (위험등급·운용사·유형·`master_fund_id`) |
| `fund_class_fees` | 판매 클래스별 보수 (운용·판매·신탁·사무관리·총보수·기타비용) |
| `fund_performance` | 연평균 수익률 (1/2/3/5년·설정이후 × 펀드/클래스/비교지수/변동성) |
| `fund_aum` | 클래스별 최근 회계기간말 잔고 (백만원 환산) |
| `fund_profiles` | 투자설명서 11개 섹션 원문 |
| `fund_sections` | 위 섹션의 청크 |

> **위험등급 표기 주의** — 국내 표준에 따라 **1등급이 가장 위험**하고 6등급이 가장 안전합니다.
> "안정적인 상품"은 `risk_grade >= 5`, "공격적인 상품"은 `risk_grade <= 2` 로 조회합니다.

> **클래스 코드 체계는 운용사마다 다릅니다** — `C-P`(연금저축)/`C-P2`(퇴직연금)/`S-R`(퇴직연금
> 온라인슈퍼) 규칙은 대다수 운용사에 적용되지만, 한화자산운용처럼 `A`/`A-E`/`C`/`C-W` 같은
> 완전히 다른 체계를 쓰는 곳도 있습니다(`diag_hanwha.py`로 실측 확인). 이런 상품은 계좌유형
> 필터로 걸러지지 않으니 "연금저축/퇴직연금 전용" 질의의 결과가 전체 상품을 다 못 덮을 수
> 있습니다 — 8장 "알려진 한계" 참고.

---

## 7. 데이터 파이프라인 설계 노트 (펀드 트랙)

원본을 구조화하면서 마주친 문제와 해결 방식입니다. 파서를 다시 손볼 때 참고용입니다.
(제도 트랙 데이터는 팀원이 별도로 구축했으므로 이 장은 펀드 트랙에 한정됩니다.)

### docx 본문·표 순서 보존

문서 형식별로 파서를 분기하면 **표가 있는 docx 가 표 전용 파서로만 라우팅되어
본문 문단이 통째로 누락**되는 사고가 납니다. `document.element.body` 를 순회하면
문단(`<w:p>`)과 표(`<w:tbl>`)가 원래 순서대로 나오므로 둘 다 살고 맥락도 유지됩니다.

### 보수표: 문자열 매칭 대신 항등식 자기검증

투자설명서 보수표는 운용사마다 컬럼 개수(5~10개)와 순서, 표 방향(행=클래스 / 열=클래스),
클래스명 위치(숫자 앞 / 뒤 / 줄머리)가 전부 다릅니다. 게다가 PDF 추출 시
`수수료미징구-오프라인` 같은 문구가 줄바꿈으로 쪼개져 헤더 매칭이 무너집니다.

그래서 문구 매칭을 버리고 **산술 항등식**으로 컬럼 위치를 확정합니다.

```
총보수 = 집합투자업자보수 + 판매회사보수 + 신탁업자보수 + 일반사무관리회사보수
보조 검증: 총보수 + 기타비용 = 총보수·비용
```

숫자 배열에서 "앞 4개의 합과 같은 값"을 찾으면 그게 총보수 열입니다. 100/100 펀드에서 동작합니다.

이 항등식은 **숫자 열 위치**는 정확히 복구하지만 **클래스명 옆 설명 문구**(`class_desc`)까지
복구하지는 못합니다. `수수료미징구-오프라인`이 줄바꿈으로 쪼개진 원본에서 숫자만 재조립되고
문구는 깨진 채로 남는 경우가 실제로 있습니다 — 그래서 `sql_agent.describe_class()`는
`class_desc`를 인용하지 않고 `class_name` 코드에서 규칙으로 의미를 유도합니다(단, 이 규칙도
모든 운용사에 보편 적용되지는 않습니다 — 위 6장 참고).

### 섹션 경계에는 반드시 종료 앵커를

`14. 이익 배분 및 과세` 처럼 시작 앵커만 두면 문서 끝(용어풀이)까지 삼킵니다.
제3부(재무정보 / 연도별 설정·환매현황 / 운용실적)를 별도 섹션으로 분리해야
**수익률과 시장잔고**를 쓸 수 있습니다.

### 표 파싱의 세 가지 함정

1. **날짜가 숫자로 읽힘** — `2024.07.08` 이 토큰 3개가 되어 열이 밀립니다. 먼저 제거합니다.
2. **각주가 데이터 행으로 잡힘** — `주 1) … 2,110,803,557 좌` 같은 줄을 건너뜁니다.
3. **단위가 문서마다 다름** — `백만` / `억좌, 억원` / `천원` / `백만좌, 백만원`.
   헤더의 단위 표기를 읽어 백만원으로 환산합니다.

### 추출기에 따라 결과가 달라지는 지점

`pdfplumber` 의 layout 모드는 `pdftotext -layout` 대비 정렬 공백이 2.4배 많습니다.
그래서 **"본문 앞 N자에서 찾기"** 같은 로직은 환경에 따라 실패합니다.
상품 유형은 본문 스캔 대신 **상품명 끝 괄호 표기**(`…자투자신탁1호(채권)`)를 우선 사용합니다.

저장용 텍스트는 공백을 줄이되(`parsers.pdf_text.tidy`), **파싱에는 원본 레이아웃을 씁니다.**

### 유니코드 NFD 파일명

맥에서 압축된 자료는 폴더·파일명이 NFD(자모 분리)로 저장됩니다.
`'투자설명서'` 라는 문자열 리터럴로는 매칭되지 않으므로,
`build_fund_db.py` 는 `R2_*.pdf` 를 품은 폴더를 직접 탐색합니다.

---

## 8. 개선 이력

평가 결과를 근거로 반영한 주요 변경입니다. 각 패치는 `--revert`로 되돌릴 수 있습니다.

**2026-08-20 1차 개선** (`apply_fixes.py`) — 원본 대비 v1 65.0%→70.0%, v2 72.7%→77.3%.
생성 프롬프트에 "수치·조건 전부 강제" 규칙 추가, 근거 없는 창작 금지 규칙 추가, 볼드가
채점기 문자열 매칭을 깨뜨리던 문제 수정, 펀드 Text2SQL 클래스 필터 보강, 연금수령한도
계산 모듈 신규 추가.

**2026-08-20 2차 회귀수정** (`apply_fixes2.py`) — Q-038 하드 에러(HCX 응답 상태코드 미확인)와
Q-039 라우팅 비결정성(클래스 코드 질문이 LLM 라우터에 맡겨져 실행마다 결과가 바뀌던 문제)을
코드 버그로 확정해 수정. "근거 없음 → 거절" 우선순위 규칙 추가.

**2026-08-22 제도 트랙 교체** — 제도(퇴직연금·연금저축 규정) 쪽 SQL/RAG를 Text-to-SQL +
벡터 유사도 방식에서 팀원(Kwonjunil) 이식 방식(결정적 슬롯 조회 + 3치 조건판정 + BM25/벡터
RRF 검색)으로 전면 교체. 펀드 트랙과 `synthesize_answer` 프롬프트는 그대로 유지.

**2026-08-22 후속 버그 수정**

- `pension_calc.py` 계산 결과를 인용할 때 산식 비율(예: 120%)도 함께 쓰도록 프롬프트 보강 (Q-030)
- 펀드 SQL 클래스 필터가 정확 일치(`=`)로 좁혀 변형 클래스를 놓치던 문제에 코드 레벨 안전장치
  추가 — 넓히면서 테이블 별칭(`fc.class_name`)을 못 잡아 SQL이 깨지던 버그도 함께 수정
- 클래스 코드가 질문에 직접 나와 필터를 안 넓혔는데 조회가 0건이면, 자동으로 넓혀 재시도하는
  로직 추가 (Q-039)
- 환각 방지 프롬프트에 "주제는 같아도 질문의 정확한 조건 조합까지 근거에 있는지 확인" 규칙
  추가. `check_hallu2.py`의 적대적 질문 6개(2개는 "실제 있는 주제 + 없는 디테일" 조합형)로
  검증 — 근거 없이 단정한 사례 0건

---

## 9. 기술 스택

| 영역 | 사용 기술 |
|---|---|
| LLM | CLOVA Studio HyperCLOVA X (HCX-007) |
| 임베딩 | CLOVA Studio Embedding v2 (1024차원) |
| 리랭킹 | CLOVA Studio Reranker |
| 제도 검색 | BM25 + 벡터 코사인, Reciprocal Rank Fusion |
| 제도 조건 판정 | 3치 논리(MET/UNMET/UNKNOWN), AST 기반 안전 수식 계산 |
| API 서버 | FastAPI + Uvicorn |
| 데이터 저장 | SQLite |
| 벡터 검색 | NumPy 코사인 유사도 (인메모리) |
| 문서 파싱 | pdfplumber · python-docx · openpyxl · python-pptx |

---

## 10. 알려진 한계

- **펀드 클래스 코드 체계가 운용사마다 다릅니다** — 대다수는 `C-P`/`C-P2`/`S-R` 패턴을 따르지만
  한화자산운용처럼 `A`/`A-E`/`C`/`C-W` 같은 완전히 다른 체계를 쓰는 운용사가 있습니다. 이런
  상품은 "연금저축/퇴직연금 전용 클래스" 필터로 걸러지지 않습니다(`diag_hanwha.py`로 확인).
  근본 해결에는 `class_desc`를 대체할 신뢰 가능한 계좌유형 컬럼이 필요한데 현재 스키마에는
  없습니다.
- **실물이전(펀드 명의만 이전) 관련 질문 일부 미해결** — "실물이전으로 옮길 수 없는 상품"
  (디폴트옵션·리츠) 류 질문은 형태소 분석(예: `실물이전으로`≠`실물이전`) 문제로 팀원 시스템도
  동일하게 실패한 것으로 확인된 항목입니다. 시간 대비 효과가 낮아 보류했습니다.
- **평가 채점 자체의 노이즈** — 문자열 매칭 채점기는 표현이 조금만 달라도 오답 처리합니다.
  같은 코드로 재실행해도 몇 문항은 뒤집힙니다. 5장의 캐비아트를 참고하세요.
- **운용실적 83/100, 설정·환매 잔고 50/100** — 운용사별 표 형식 차이로 자동 추출이 되지 않는
  문서가 남아 있습니다. 잔고는 원본에 `신규설정으로 해당사항 없음` 인 펀드가 26건 포함된 수치입니다.
- **스캔 PDF 는 기존 OCR 결과를 유지** — 텍스트 레이어가 없는 문서는 CLOVA OCR 로 처리된
  레코드를 그대로 사용합니다. 오프라인 재생성은 불가능합니다.
- **클래스명 표기 흔들림** — 1,053행 중 약 5% 에서 `C-E` 를 `CE` 로 읽는 등의 편차가 있습니다.
  같은 클래스명이 중복되면 `A#2` 형태로 번호를 붙여 구분합니다.

---

## 11. 라이선스 및 고지

본 저장소는 제10회 2026 미래에셋증권 AI Festival 출품을 위한 것입니다.
연금 제도 문서 및 펀드 투자설명서의 저작권은 각 발행 기관에 있으며 저장소에 포함하지 않습니다.

제도 트랙의 정형 DB(`pension_rules.db`)와 임베딩 인덱스(`junil_rag_embeddings_v3.json`),
그리고 결정적 슬롯 조회·3치 조건판정·BM25 랭킹 코드(`institution_sql_agent.py`,
`institution_rag_agent.py`, `tri.py`, `safe_eval.py`, `bm25.py`, `budget.py`)는
같은 대회를 준비한 팀원(Kwonjunil)의 저장소(`Kwonjunil/mirae_asset_competiton`)에서
이식했습니다.
