# 연금 상담 AI 에이전트

> 미래에셋 & 네이버 AI Festival 2026 출품작 — 연금 주제
> CLOVA Studio(HCX-007) 기반 **하이브리드 RAG + Text-to-SQL** 연금 상담 에이전트

---

## 1. 개요

퇴직연금·개인연금 관련 질의에 대해 **제도 규정(비정형 문서)** 과 **수치 데이터(정형 DB)** 를
동시에 근거로 삼아 답변하는 에이전트입니다.

질문의 성격을 LLM이 먼저 분류한 뒤, 필요한 검색 경로만 선택적으로 태워
"약관 설명"과 "수치 비교"를 하나의 답변으로 통합합니다.

**설계 원칙**

- **역질문 금지** — 조건이 누락된 질의는 되묻지 않고, 가능한 경우의 수를 조건별로 분기해 한 번에 완결 답변
- **근거 기반** — 검색된 원문에 없는 수치는 생성하지 않음
- **추적 가능성** — 답변에 사용된 청크의 출처 파일·페이지를 함께 반환

---

## 2. 아키텍처

```
                    GET /answer?question_id=&question=
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │  PensionOrchestrator   │
                    └────────────────────────┘
                                 │
                    ① 의도 분류 (HCX-007 Router)
                    SQL_FIN / SQL_FUND / HYBRID / RAG
                                 │
                ┌────────────────┴────────────────┐
                ▼                                 ▼
      ┌──────────────────┐              ┌──────────────────┐
      │  PensionSQLAgent │              │  PensionRAGAgent │
      │   (Text-to-SQL)  │              │  (Hybrid Search) │
      └──────────────────┘              └──────────────────┘
                │                                 │
      ┌─────────┴─────────┐          ② CLOVA Embedding v2 (1024d)
      ▼                   ▼          ③ 벡터 유사도 + 메타데이터 보너스
financial_data      fund_prospectus  ④ CLOVA Reranker (Top-K 재정렬)
  .sqlite               .sqlite                   │
 (제도·세제)          (펀드 100종)                 ▼
                                        근거 청크 + 출처 메타
                │                                 │
                └────────────────┬────────────────┘
                                 ▼
                    ⑤ HCX-007 통합 답변 생성
                                 │
                                 ▼
        { question_id, question, retrieved_context,
          think_trace, answer }
```

### 검색 전략: 메타데이터 가중 하이브리드 서치

순수 벡터 유사도만으로는 "IRP 중도인출"과 "DC 중도인출"처럼 표현이 비슷하지만
적용 제도가 다른 문서를 구분하지 못합니다. 이를 보완하기 위해
질의에서 추출한 메타데이터와 청크 태그의 일치도를 점수에 가산합니다.

```
hybrid_score = cosine_similarity(query, chunk)
             + 0.08 × (연금 유형 일치)
             + 0.06 × (주제 일치)
             + 0.05 × (지식 속성 일치 개수)
             + 0.03 × (키워드 포함)
             - 0.08 × (질의에 유형 언급이 없는데 특수 유형 문서인 경우)
```

---

## 3. 프로젝트 구조

```
.
├── main.py                 # FastAPI 엔트리포인트 (공식 평가 스키마)
├── orchestrator.py         # 라우팅 → 검색 → 통합 생성 오케스트레이션
├── rag_agent.py            # 임베딩 · 하이브리드 검색 · 리랭킹 · 답변 생성
├── sql_agent.py            # Text-to-SQL 생성 및 실행
├── build_sql_db.py         # 제도·세제 정형 DB 구축
├── build_fund_db.py        # 펀드 투자설명서 DB 구축
├── knowledge_base_final.json  # 파싱된 제도 문서 지식베이스 (599 레코드)
├── evaluation_set.json     # 자체 평가 질의셋
├── requirements.txt
└── .env.example
```

### 저장소에 포함되지 않은 항목

용량 문제로 아래는 제외되어 있습니다. `.gitignore` 참고.

| 경로 | 내용 | 생성 방법 |
|---|---|---|
| `docs/` | 제도 문서 원본 PDF 58건 | 별도 배포 |
| `funds/` | 펀드 투자설명서 PDF 50건 | 별도 배포 |
| `product_profiles.json` | 펀드 프로필 파싱 결과 | 별도 배포 |
| `product_sections.json` | 펀드 섹션 청크 | 별도 배포 |
| `data/financial_data.sqlite` | 제도·세제 정형 DB | `python build_sql_db.py` |
| `data/fund_prospectus.sqlite` | 펀드 DB | `python build_fund_db.py` |
| `data/vector_db/rag_embeddings_v2.json` | 임베딩 인덱스 | 임베딩 스크립트 |

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

`.env` 파일을 열어 CLOVA Studio API 키를 입력합니다.

```
CLOVASTUDIO_API_KEY=nv-xxxxxxxxxxxxxxxx
```

> `.env`는 `.gitignore`에 등록되어 있습니다. 키를 커밋하지 마세요.

### 4.3 데이터베이스 구축

```bash
python build_sql_db.py     # → data/financial_data.sqlite
python build_fund_db.py    # → data/fund_prospectus.sqlite
```

### 4.4 서버 실행

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4.5 호출 예시

```bash
curl "http://localhost:8000/answer?question_id=Q-001&question=연금저축이랑 IRP 다 합쳐서 세액공제 얼마까지 되나요?"
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
| 제도 문서 | PDF 57건 + PPTX 1건 |
| 지식베이스 레코드 | 599건 |
| 임베딩 청크 | 634건 (1024차원) |
| 펀드 투자설명서 | 50건 |
| 펀드 마스터 | 100종 |
| 펀드 섹션 청크 | 6,330건 |

### 정형 DB 스키마

**`financial_data.sqlite`** — 제도·세제

- `pension_tax_limits` — 소득 구간별 세액공제 한도 및 공제율
- `pension_age_tax_rates` — 연령대별 연금소득세율 (55~69세 5.5% / 70~79세 4.4% / 80세~ 3.3%)
- `pension_seizure_rules` — 자산 유형별 압류 가능 여부 및 법적 근거
- `pension_withdrawal_rules` — 중도인출 법정 사유별 과세율
- `document_tables` — 문서에서 추출한 표 원문

**`fund_prospectus.sqlite`** — 펀드

- `fund_products` — 펀드 마스터 (위험등급, 운용사, 유형)
- `fund_profiles` — 투자목적·전략·위험 등 7대 항목 원문
- `fund_class_fees` — 클래스별 보수·수수료 정형 데이터
- `fund_sections` — 투자설명서 섹션 청크

> **위험등급 표기 주의** — 국내 표준에 따라 **1등급이 가장 위험**하고 6등급이 가장 안전합니다.

---

## 6. 평가

`evaluation_set.json`에 난이도·카테고리별 질의와 기대 키워드, 예상 처리 엔진이 정의되어 있습니다.

| ID | 난이도 | 카테고리 | 예상 엔진 |
|---|---|---|---|
| Q-001 | 하 | 세제 | SQLite |
| Q-002 | 중 | 과세/세율 | SQLite |
| Q-003 | 중 | 상품 필터링 | SQLite |
| Q-004 | 중 | 법적 권리/압류 | SQLite |
| Q-005 | 하 | 제도 비교 | Vector DB |
| Q-006 | 상 | 절세/복합 상담 | 하이브리드 |

---

## 7. 기술 스택

| 영역 | 사용 기술 |
|---|---|
| LLM | CLOVA Studio HCX-007 |
| 임베딩 | CLOVA Studio Embedding v2 (1024차원) |
| 리랭킹 | CLOVA Studio Reranker |
| API 서버 | FastAPI + Uvicorn |
| 데이터 저장 | SQLite |
| 벡터 검색 | NumPy 코사인 유사도 (인메모리) |

---

## 8. 라이선스 및 고지

본 저장소는 미래에셋 & 네이버 AI Festival 2026 출품을 위한 것입니다.
포함된 연금 제도 문서 및 펀드 투자설명서의 저작권은 각 발행 기관에 있습니다.
