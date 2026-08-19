# 연금 상담 AI 에이전트

> 제10회 2026 미래에셋증권 AI Festival 출품작 — 연금 Agent
> CLOVA Studio(HyperCLOVA X) 기반 **하이브리드 RAG + Text-to-SQL** 연금 상담 에이전트

---

## 1. 개요

퇴직연금(DB·DC·IRP)과 개인연금(연금저축) 질의에 대해 **제도 규정(비정형 문서)** 과
**상품 수치(정형 DB)** 를 동시에 근거로 삼아 답변하는 에이전트입니다.

질문의 성격을 먼저 분류한 뒤 필요한 검색 경로만 선택적으로 태워,
"약관이 어떻게 되어 있는가"와 "그래서 어떤 상품이 얼마인가"를 하나의 답변으로 통합합니다.

### 설계 원칙

**단일턴 완결성** — 평가가 단일턴으로 진행되므로 되묻고 기다릴 수 없습니다.
조건이 부족하면 확인 질문을 **첫 답변 안에 포함하고 동시에 조건별 결론까지** 제시합니다.
(`Clarify` 대신 `Assume + Expose` 전략)

**근거 고정** — 금액·세율·한도·보수율은 검색된 원문의 값을 변형 없이 인용합니다.
근거에 없는 사실은 생성하지 않고, 답할 수 없으면 한계를 명시합니다.

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
                    (1) 의도 분류 (HCX-007 Router)
                       SQL_FIN / SQL_FUND / HYBRID / RAG
                                 |
                +----------------+----------------+
                |                                 |
      +------------------+              +------------------+
      |  PensionSQLAgent |              |  PensionRAGAgent |
      |   (Text-to-SQL)  |              |  (Hybrid Search) |
      +------------------+              +------------------+
                |                                 |
      +---------+---------+          (2) CLOVA Embedding v2 (1024d)
      |                   |          (3) 벡터 유사도 + 메타데이터 가산점
financial_data      fund_prospectus  (4) CLOVA Reranker (Top-K 재정렬)
  .sqlite             _v2.sqlite                  |
 (제도/세제)          (펀드 100종)          근거 청크 + 출처 메타
                |                                 |
                +----------------+----------------+
                                 |
                    (5) HCX-007 통합 답변 생성
                                 |
        { question_id, question, retrieved_context,
          think_trace, answer }
```

### 검색 전략: 메타데이터 가중 하이브리드 서치

순수 벡터 유사도만으로는 "IRP 중도인출"과 "DC 중도인출"처럼 표현이 비슷하지만
적용 제도가 다른 문서를 구분하지 못합니다. 질의에서 추출한 태그와 청크 태그의
일치도를 점수에 가산해 이를 보완합니다.

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

---

## 3. 프로젝트 구조

```
.
├── main.py                     FastAPI 엔트리포인트 (공식 평가 스키마)
├── orchestrator.py             라우팅 -> 검색 -> 통합 생성
├── rag_agent.py                임베딩 · 하이브리드 검색 · 리랭킹 · 답변 생성
├── sql_agent.py                Text-to-SQL 생성 및 실행
│
├── parsers/                    원본 문서 파서 모듈
│   ├── pdf_text.py             PDF 텍스트 추출 (pdftotext -> pdfplumber 자동 대체 + 캐시)
│   ├── docx_reader.py          docx 문단·표를 등장 순서 그대로 읽기
│   ├── legal_reader.py         안내문 말미 관련법령 조문 추출
│   ├── fund_fees.py            투자설명서 보수표 파서 (항등식 자기검증)
│   └── fund_sections.py        투자설명서 섹션 분할 + 운용실적 · 설정환매 추출
│
├── build_knowledge_base.py     제도 문서 -> knowledge_base_v2.json
├── build_fund_db.py            투자설명서 -> product_*_v2.json, fund_prospectus_v2.sqlite
├── build_sql_db.py             제도·세제 정형 DB -> financial_data.sqlite
├── build_embeddings.py         임베딩 인덱스 -> rag_embeddings_v3.json   (CLOVA API 필요)
├── verify_data.py              재생성 결과 검증 리포트
│
├── knowledge_base_v2.json      파싱된 제도 문서 지식베이스 (712 레코드)
├── evaluation_set.json         자체 평가 질의셋
├── requirements.txt
└── .env.example
```

### 저장소에 포함되지 않은 항목

용량과 배포 제한 때문에 아래는 제외되어 있습니다. `.gitignore` 참고.

| 경로 | 내용 | 확보 방법 |
|---|---|---|
| `docs_renamed/` | 제도 문서 원본 58건 (pdf 37 · docx 18 · xlsx 2 · pptx 1) | 주최측 제공 |
| `투자설명서/` | 펀드 투자설명서 100건 | 주최측 제공 |
| `product_profiles_v2.json` | 펀드 프로필 파싱 결과 (14MB) | `python build_fund_db.py` |
| `product_sections_v2.json` | 펀드 섹션 청크 (27MB) | `python build_fund_db.py` |
| `data/*.sqlite` | 정형 DB | build 스크립트 |
| `data/vector_db/rag_embeddings_v3.json` | 임베딩 인덱스 (10MB) | `python build_embeddings.py` |
| `.cache_pdftext/`, `.cache_embed.json` | 재실행 가속용 캐시 | 자동 생성 |

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

원본 폴더(`docs_renamed/`, `투자설명서/`)를 프로젝트 루트에 놓고 아래 순서로 실행합니다.

```bash
python build_knowledge_base.py    # 제도 지식베이스        (수 초)
python build_fund_db.py           # 펀드 DB               (최초 20~25분, 이후 1~2분)
python build_sql_db.py            # 제도·세제 정형 DB      (수 초)
python verify_data.py             # 검증 리포트            (수 초)
python build_embeddings.py        # 임베딩 인덱스          (CLOVA API 필요)
```

**`build_fund_db.py` 소요 시간** — `pdftotext`(poppler)가 설치돼 있으면 30초,
없으면 `pdfplumber` 로 100건을 추출하느라 20~25분 걸립니다.
추출 결과는 `.cache_pdftext/` 에 캐시되므로 두 번째 실행부터는 1~2분입니다.

**`build_embeddings.py` 는 마지막에 한 번만** — 기존 인덱스에 같은 본문이 있으면
벡터를 재사용하므로 실제 API 호출은 신규 레코드 수만큼만 발생합니다.
호출 한도(429)로 일부가 실패해도 성공분은 `.cache_embed.json` 에 저장되므로,
다시 실행하면 실패분만 재시도합니다.

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
  "think_trace": "1. 의도 분류 및 라우팅: SQL_FIN -> 2. 정형 DB 조회 완료 ...",
  "answer": "..."
}
```

---

## 5. 데이터 현황

| 구분 | 규모 |
|---|---|
| 제도 문서 원본 | 58건 (PDF 37 · docx 18 · xlsx 2 · pptx 1) |
| 지식베이스 레코드 | **712건** |
| 임베딩 청크 | **700건** (1024차원) |
| 펀드 투자설명서 | 100건 (고유 92건) |
| 펀드 마스터 | 100종 |
| 클래스별 보수 | **1,053행** |
| 운용실적(수익률) | **6,192행** (83종) |
| 설정·환매 잔고 | **150행** (50종) |
| 펀드 섹션 청크 | 5,328건 |

### 정형 DB 스키마

**`data/financial_data.sqlite`** — 제도·세제

| 테이블 | 내용 |
|---|---|
| `pension_tax_limits` | 소득 구간별 세액공제 한도 및 공제율 |
| `pension_age_tax_rates` | 연령대별 연금소득세율 (55~69세 5.5% / 70~79세 4.4% / 80세~ 3.3%) |
| `pension_seizure_rules` | 자산 유형별 압류 가능 여부 및 법적 근거 |
| `pension_withdrawal_rules` | 중도인출 법정 사유별 과세율 |
| `document_tables` | 문서에서 추출한 표 원문 |

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

---

## 6. 데이터 파이프라인 설계 노트

원본을 구조화하면서 마주친 문제와 해결 방식입니다. 파서를 다시 손볼 때 참고용입니다.

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

## 7. 기술 스택

| 영역 | 사용 기술 |
|---|---|
| LLM | CLOVA Studio HyperCLOVA X (HCX-007) |
| 임베딩 | CLOVA Studio Embedding v2 (1024차원) |
| 리랭킹 | CLOVA Studio Reranker |
| API 서버 | FastAPI + Uvicorn |
| 데이터 저장 | SQLite |
| 벡터 검색 | NumPy 코사인 유사도 (인메모리) |
| 문서 파싱 | pdfplumber · python-docx · openpyxl · python-pptx |

---

## 8. 알려진 한계

- **운용실적 83/100, 설정·환매 잔고 50/100** — 운용사별 표 형식 차이로 자동 추출이 되지 않는
  문서가 남아 있습니다. 잔고는 원본에 `신규설정으로 해당사항 없음` 인 펀드가 26건 포함된 수치입니다.
- **스캔 PDF 는 기존 OCR 결과를 유지** — 텍스트 레이어가 없는 문서는 CLOVA OCR 로 처리된
  레코드를 그대로 사용합니다. 오프라인 재생성은 불가능합니다.
- **클래스명 표기 흔들림** — 1,053행 중 약 5% 에서 `C-E` 를 `CE` 로 읽는 등의 편차가 있습니다.
  같은 클래스명이 중복되면 `A#2` 형태로 번호를 붙여 구분합니다.

---

## 9. 라이선스 및 고지

본 저장소는 제10회 2026 미래에셋증권 AI Festival 출품을 위한 것입니다.
연금 제도 문서 및 펀드 투자설명서의 저작권은 각 발행 기관에 있으며 저장소에 포함하지 않습니다.
