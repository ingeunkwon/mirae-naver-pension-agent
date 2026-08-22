#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
평가 분석(2026-08-20) 결과에 따른 개선 패치 적용 스크립트.

원본은 *.bak_20260820 으로 백업한 뒤 수정한다.
모든 치환은 "정확히 1회 등장"을 검증하고, 하나라도 실패하면 아무 파일도 쓰지 않는다.

    python apply_fixes.py            # 적용
    python apply_fixes.py --check    # 무엇이 바뀔지만 확인 (쓰기 없음)
    python apply_fixes.py --revert   # 백업으로 되돌리기
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BAK = ".bak_20260820"

# ══════════════════════════════════════════════════════════════════════
# rag_agent.py
# ══════════════════════════════════════════════════════════════════════

RAG = []

# ── ① CLOVA 호출 재시도 헬퍼 ─────────────────────────────────────────
RAG.append((
"""def build_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
""",
'''def build_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


# CLOVA 가 429(호출 한도)나 5xx 를 한 번 뱉으면 그 문항이 통째로 0점이 된다.
# 평가는 40~66문항을 연속으로 던지므로 한 번은 반드시 만난다고 봐야 한다.
# 지수 백오프로 재시도하고, 그래도 안 되면 마지막 응답을 그대로 돌려준다
# (호출부가 status_code 로 판단하도록 예외를 삼키지 않는다).
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def post_with_retry(url, payload, timeout=TIMEOUT, tries=3, backoff=2.0):
    import time as _time
    last = None
    for attempt in range(tries):
        try:
            last = requests.post(url, headers=build_headers(),
                                 json=payload, timeout=timeout)
            if last.status_code not in RETRY_STATUS:
                return last
        except requests.RequestException as e:
            last = None
            err = e
        if attempt < tries - 1:
            _time.sleep(backoff * (2 ** attempt))
    if last is None:
        raise RuntimeError(f"CLOVA 호출 실패({tries}회 재시도): {err}")
    return last
''',
))

# ── ② 임베딩·리랭커·생성 호출을 재시도 경로로 ────────────────────────
RAG.append((
"""    response = requests.post(
        EMBEDDING_URL,
        headers=build_headers(),
        json={"text": text},
        timeout=TIMEOUT,
    )
""",
"""    response = post_with_retry(EMBEDDING_URL, {"text": text})
""",
))

RAG.append((
"""    response = requests.post(
        RERANKER_URL,
        headers=build_headers(),
        json={"query": query, "documents": documents, "maxTokens": 500},
        timeout=TIMEOUT,
    )
""",
"""    response = post_with_retry(
        RERANKER_URL,
        {"query": query, "documents": documents, "maxTokens": 500},
    )
""",
))

RAG.append((
"""    response = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"HCX 오류 {response.status_code}: {response.text[:1000]}")
""",
"""    response = post_with_retry(HCX_URL, payload)
    if response.status_code != 200:
        raise RuntimeError(f"HCX 오류 {response.status_code}: {response.text[:1000]}")
""",
))

# ── ③ 근거 관련성 임계값 상수 ────────────────────────────────────────
RAG.append((
"""TIMEOUT = 90
HYBRID_TOP_K = 10
""",
'''TIMEOUT = 90
HYBRID_TOP_K = 10

# 코퍼스에 없는 질문인데도 hybrid_search 는 항상 top_k 개를 돌려준다.
# 관련성 임계값이 없으면 무관한 청크가 근거로 올라가고, 생성 모델이 그 위에
# 이야기를 만든다(평가 v2 환각 점검 1/4, V-10/V-21/V-26).
# 상위 문서의 코사인 유사도가 이 값 미만이면 생성을 건너뛰고 한계를 고지한다.
#   0.0 = 비활성(기본). calibrate_threshold.py 로 분포를 재고 값을 정한 뒤 켠다.
MIN_TOP_SCORE = float(os.getenv("RAG_MIN_TOP_SCORE", "0.0"))
''',
))

# ── ④ ask() 에 임계값 게이트 ─────────────────────────────────────────
RAG.append((
"""    def ask(self, query, generate=True):
        hybrid_results = hybrid_search(query, self.records, top_k=HYBRID_TOP_K)
        reranker_result = rerank(query, hybrid_results)
        context, evidence_map = build_context(reranker_result)

        if not context:
""",
'''    def ask(self, query, generate=True):
        hybrid_results = hybrid_search(query, self.records, top_k=HYBRID_TOP_K)

        # 상위 문서조차 유사도가 낮으면 코퍼스가 이 주제를 안 다루는 것이다.
        # 리랭커·생성을 태우지 않고 여기서 끊는다(환각 방지 + 호출 절약).
        top_score = hybrid_results[0]["vector_score"] if hybrid_results else 0.0
        self.last_top_score = top_score
        if MIN_TOP_SCORE > 0 and top_score < MIN_TOP_SCORE:
            return {
                "query": query, "answer": "",
                "sources": [], "used_evidence": [],
                "clarifications": [], "assumptions": [],
                "raw_context": "",
                "below_threshold": True,
                "top_score": top_score,
            }

        reranker_result = rerank(query, hybrid_results)
        context, evidence_map = build_context(reranker_result)

        if not context:
''',
))

# ── ⑤ 생성 프롬프트: 규칙 3(수치·조건 전부) + 환각 차단 강화 ─────────
RAG.append((
"""3) 근거
   금액, 세율, 기간, 연령, 한도, 보수율은 근거에 적힌 값을 변형 없이 인용한다.
   어느 근거에서 나온 수치인지 문장 안에서 드러나게 쓴다.
4) 다음 행동
   사용자가 이어서 확인하거나 결정할 일을 한 줄로 제시한다.
""",
"""3) 근거
   금액, 세율, 기간, 연령, 한도, 보수율은 근거에 적힌 값을 변형 없이 인용한다.
   어느 근거에서 나온 수치인지 문장 안에서 드러나게 쓴다.
4) 다음 행동
   사용자가 이어서 확인하거나 결정할 일을 한 줄로 제시한다.

[규칙 - 수치와 조건은 절대 요약하지 않는다]  ★ 가장 중요
근거에 수치나 조건이 여러 개 나열되어 있거나 경우에 따라 다르게 제시되어 있으면,
임의로 하나만 고르거나 뭉뚱그리지 말고 전부 다 적는다.
아래 항목은 하나도 빠뜨리지 않는다.
  - 금액.비율.세율   (900만원, 1,800만원, 16.5%, 13.2%, 4.4% ...)
  - 기한.기간        (60일 이내, 6개월 이상, 14일 이내, 6주, 5년 경과 ...)
  - 조건.자격        (무주택자, 만 55세 이상, 계속근로기간 1년 이상, 주 15시간 ...)
  - 절차.방법        (내점 신청, 신청서 제출, 근로자대표 동의, 고용노동부 신고 ...)
  - 예외.단서        ("단, ~인 경우는 제외" 같은 문구)
요약하느라 이 항목을 생략하면 답변이 틀린 것으로 간주된다.
근거에 두 가지 경우가 나오면 둘 다 쓴다. 길이 제한은 없다.

[규칙 - 근거가 주제를 안 다루면 만들지 않는다]
검색 근거가 질문의 주제를 직접 다루지 않으면 아는 것처럼 답을 구성하지 않는다.
  1. 근거에 없는 절차.기한.연락처.URL.기관명.법령 조문을 만들어내지 않는다.
  2. 실제로 그 내용을 담고 있지 않은 근거 번호를 인용하지 않는다.
     [근거 N] 을 쓸 때는 그 근거에 해당 문장이 실제로 있어야 한다.
  3. "일반적으로", "대부분의 기관에서는", "통상" 으로 시작하는 일반론으로
     근거의 빈자리를 메우지 않는다.
근거가 주제를 다루지 않으면 이렇게만 답한다.
  "제공된 자료에서는 OO에 대한 내용을 확인하지 못했습니다."
  그 뒤에 확인 가능한 인접 정보가 있으면 그것만 덧붙인다.
""",
))

# ── ⑥ 출력 형식: 용어 중간 볼드 금지 ─────────────────────────────────
RAG.append((
"""[출력 형식 주의]
answer 값 안에서 LaTeX 표기를 쓰지 않는다. 백슬래시가 JSON 을 깨뜨린다.
곱셈은 x, 퍼센트는 % 로 그대로 쓴다.""",
"""[출력 형식 주의]
용어나 수치 중간에 ** 를 넣지 않는다. 강조는 항목 이름이나 줄머리에만 쓴다.
용어와 조사 사이에 ** 가 끼면 문자열이 끊어져 채점.검색에서 불이익이 있다.
  (X) 자금을 **금융기관**에 적립하고      (O) 자금을 금융기관에 적립하고
  (X) 이전하는 것은 **불가능**합니다      (O) 이전하는 것은 불가능합니다
  (X) 세액공제율은 **13.2%**입니다        (O) 세액공제율은 13.2% 입니다

answer 값 안에서 LaTeX 표기를 쓰지 않는다. 백슬래시가 JSON 을 깨뜨린다.
곱셈은 x, 퍼센트는 % 로 그대로 쓴다.""",
))


# ══════════════════════════════════════════════════════════════════════
# orchestrator.py
# ══════════════════════════════════════════════════════════════════════

ORC = []

ORC.append((
"from rag_agent import PensionRAGAgent, build_headers, TIMEOUT, HCX_URL",
"from rag_agent import (PensionRAGAgent, build_headers, post_with_retry,\n"
"                       TIMEOUT, HCX_URL)\n"
"import pension_calc",
))

# 연금수령한도는 코드로 계산한다 (Q-030: 검색이 빈손이라 답변 자체를 포기했던 문항)
ORC.append((
"""        # 3. 답변 생성
        if route == "RAG" and rag_res.get("answer"):""",
"""        # 2-b. 정해진 공식이 있는 계산은 LLM 에게 시키지 않는다.
        # HCX 가 x120% 를 x(11-연차) 로 잘못 적용해 8배 틀린 값을 내는 사례가 있다.
        calc_text = pension_calc.compute(query)
        if calc_text:
            sql_context = (calc_text + "\\n\\n" + sql_context).strip()
            think_trace_list.append(
                "3-3. 연금수령한도를 코드로 직접 계산 (LLM 산수 오류 회피)")

        # 3. 답변 생성
        # 계산 결과가 있으면 RAG 직답 대신 통합 생성으로 보내 계산값을 반드시 싣는다.
        if route == "RAG" and rag_res.get("answer") and not calc_text:""",
))

ORC.append((
"""            res = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT).json()
            content = res.get("result", {}).get("message", {}).get("content", "").strip()""",
"""            res = post_with_retry(HCX_URL, payload).json()
            content = res.get("result", {}).get("message", {}).get("content", "").strip()""",
))

ORC.append((
"""        res = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT).json()
        return res.get("result", {}).get("message", {}).get("content", "").strip()""",
"""        res = post_with_retry(HCX_URL, payload).json()
        return res.get("result", {}).get("message", {}).get("content", "").strip()""",
))

# 규칙 3 + 환각 차단
ORC.append((
"""3) 근거
   금액, 세율, 한도, 보수율, 위험등급, 수익률은 근거에 적힌 값을 변형 없이 인용한다.
   제도 근거와 DB 조회 수치를 함께 제시해 어디서 나온 값인지 드러낸다.
4) 다음 행동
   사용자가 이어서 확인하거나 결정할 일을 한 줄로 제시한다.
""",
"""3) 근거
   금액, 세율, 한도, 보수율, 위험등급, 수익률은 근거에 적힌 값을 변형 없이 인용한다.
   제도 근거와 DB 조회 수치를 함께 제시해 어디서 나온 값인지 드러낸다.
4) 다음 행동
   사용자가 이어서 확인하거나 결정할 일을 한 줄로 제시한다.

[규칙 - 수치와 조건은 절대 요약하지 않는다]  ★ 가장 중요
근거에 수치나 조건이 여러 개 나열되어 있거나 경우에 따라 다르게 제시되어 있으면,
임의로 하나만 고르거나 뭉뚱그리지 말고 전부 다 적는다.
아래 항목은 하나도 빠뜨리지 않는다.
  - 금액.비율.세율   (900만원, 1,800만원, 16.5%, 13.2%, 4.4% ...)
  - 기한.기간        (60일 이내, 6개월 이상, 14일 이내, 6주, 5년 경과 ...)
  - 조건.자격        (무주택자, 만 55세 이상, 계속근로기간 1년 이상, 주 15시간 ...)
  - 절차.방법        (내점 신청, 신청서 제출, 근로자대표 동의, 고용노동부 신고 ...)
  - 예외.단서        ("단, ~인 경우는 제외" 같은 문구)
요약하느라 이 항목을 생략하면 답변이 틀린 것으로 간주된다.
근거에 두 가지 경우가 나오면 둘 다 쓴다. 길이 제한은 없다.

[규칙 - SQL 조회 결과를 임의로 잘라내지 않는다]
정형 DB 조회 결과가 여러 행이면 질문이 요구한 범위 안의 행을 전부 제시한다.
"총 N개 중 일부 발췌" 처럼 임의로 줄이지 않는다. 표로 정리하면 읽기 좋다.

[규칙 - 근거가 주제를 안 다루면 만들지 않는다]
검색 근거가 질문의 주제를 직접 다루지 않으면 아는 것처럼 답을 구성하지 않는다.
  1. 근거에 없는 절차.기한.연락처.URL.기관명.법령 조문을 만들어내지 않는다.
  2. 실제로 그 내용을 담고 있지 않은 근거 번호를 인용하지 않는다.
  3. "일반적으로", "대부분의 기관에서는", "통상" 으로 시작하는 일반론으로
     근거의 빈자리를 메우지 않는다.
근거가 주제를 다루지 않으면 "제공된 자료에서는 OO에 대한 내용을 확인하지
못했습니다." 로만 답하고, 확인 가능한 인접 정보가 있으면 그것만 덧붙인다.

[출력 형식 주의]
용어나 수치 중간에 ** 를 넣지 않는다. 강조는 항목 이름이나 줄머리에만 쓴다.
용어와 조사 사이에 ** 가 끼면 문자열이 끊어져 채점.검색에서 불이익이 있다.
  (X) 자금을 **금융기관**에 적립하고      (O) 자금을 금융기관에 적립하고
  (X) 이전하는 것은 **불가능**합니다      (O) 이전하는 것은 불가능합니다
""",
))

# HYBRID 에서 DB 를 하나만 고르던 것 → 세제 신호가 있으면 FIN 도 함께
# (두 조각으로 나눈 이유: 사이 빈 줄에 후행 공백이 있어 한 덩어리로는 안 잡힌다)
ORC.append((
"""        if route in ["SQL_FIN", "SQL_FUND", "HYBRID"]:
            db_type = "FIN" if route == "SQL_FIN" else "FUND"
            if route == "HYBRID":
                db_type = "FUND" if any(k in query for k in ["펀드", "상품", "보수", "등급", "추천"]) else "FIN"
""",
"""        # HYBRID 는 원래 DB 를 하나만 골랐다. "세액공제 받으면서 넣을 저보수 연금펀드"
        # 처럼 양쪽이 다 필요한 질의에서 한쪽 수치가 통째로 빠졌다.
        # 신호가 둘 다 있으면 둘 다 조회한다.
        if route in ["SQL_FIN", "SQL_FUND", "HYBRID"]:
            if route == "SQL_FIN":
                db_types = ["FIN"]
            elif route == "SQL_FUND":
                db_types = ["FUND"]
            else:
                _q = query.lower()
                want_fund = any(w in _q for w in self.FUND_WORDS)
                want_fin = any(w in _q for w in self.FIN_WORDS)
                db_types = [t for t, w in (("FUND", want_fund), ("FIN", want_fin)) if w]
                if not db_types:
                    db_types = ["FIN"]
""",
))

ORC.append((
"""            sql_res = self.sql_agent.generate_and_execute(query, db_type)
            sql_context = json.dumps(sql_res, ensure_ascii=False)
            think_trace_list.append(f"2. 정형 DB 조회 완료 (SQL: {sql_res.get('sql', 'N/A')})")
            retrieved_sources.append({"source_file": f"{db_type.lower()}_data.sqlite"})
""",
"""            sql_parts = []
            for db_type in db_types:
                sql_res = self.sql_agent.generate_and_execute(query, db_type)
                sql_parts.append(json.dumps(sql_res, ensure_ascii=False))
                think_trace_list.append(
                    f"2. 정형 DB 조회 완료 [{db_type}] (SQL: {sql_res.get('sql', 'N/A')})")
                retrieved_sources.append({"source_file": f"{db_type.lower()}_data.sqlite"})
            sql_context = "\\n".join(sql_parts)
""",
))


# ══════════════════════════════════════════════════════════════════════
# sql_agent.py
# ══════════════════════════════════════════════════════════════════════

SQL = []

SQL.append((
"from rag_agent import build_headers, TIMEOUT, HCX_URL",
"from rag_agent import build_headers, post_with_retry, TIMEOUT, HCX_URL",
))

SQL.append((
"""            res = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT).json()""",
"""            res = post_with_retry(HCX_URL, payload).json()""",
))

# 클래스 코드 규칙 — class_desc 가 깨져 있어 코드로 유도해야 한다
SQL.append((
"""2. fund_class_fees (product_code TEXT, class_name TEXT, class_desc TEXT,
     mgmt_fee REAL, sales_fee REAL, trust_fee REAL, admin_fee REAL,
     total_fee REAL, etc_cost REAL, total_cost REAL)
   -- 판매 클래스별 보수. 단위는 연 %. total_fee 가 총보수(TER).
      class_name 예: A(수수료선취-오프라인), A-E(온라인), C(수수료미징구-오프라인),
      C-P(개인연금), C-PE(온라인 개인연금), C-P2/C-R(퇴직연금), S(온라인슈퍼)
      연금계좌 질문이면 C-P / C-PE / C-P2 / C-R 계열을 우선 본다.
""",
"""2. fund_class_fees (product_code TEXT, class_name TEXT, class_desc TEXT,
     mgmt_fee REAL, sales_fee REAL, trust_fee REAL, admin_fee REAL,
     total_fee REAL, etc_cost REAL, total_cost REAL)
   -- 판매 클래스별 보수. 단위는 연 %. total_fee 가 총보수(TER).

   ★ 클래스 코드 해석 규칙 (class_desc 는 PDF 줄바꿈으로 문자열이 깨져 있으므로
     절대 인용하지 말고, 아래 규칙으로 설명한다)
       P  계열  (C-P, C-PE, S-P, C-P1 ...)        -> 연금저축(개인연금)
       P2 계열  (C-P2, C-P2E, S-P2, C-P2I ...)    -> 퇴직연금
       R  계열  (C-R, C-RJ, C-RF, C-RI ...)       -> 퇴직연금
       코드 끝에 E 가 붙으면 온라인, 없으면 오프라인. S 로 시작하면 온라인슈퍼.
       A = 수수료선취, C = 수수료미징구.
     예) C-P  = 수수료미징구-오프라인-개인연금(연금저축)
         C-PE = 수수료미징구-온라인-개인연금(연금저축)
         C-P2 = 수수료미징구-오프라인-퇴직연금
         C-P2E= 수수료미징구-온라인-퇴직연금

   ★ 클래스 필터는 반드시 LIKE 로 쓴다. 정확히 일치(=)로 좁히면 변형 클래스를
     통째로 놓친다. 실제로 'C-P2' 로만 좁혀 최저 보수 펀드를 놓친 사고가 있었다.
       (X) WHERE class_name = 'C-P2'
       (O) 퇴직연금:  WHERE (class_name LIKE '%P2%' OR class_name LIKE '%-R%')
       (O) 연금저축:  WHERE (class_name LIKE '%-P%' AND class_name NOT LIKE '%P2%')
       (O) 연금계좌 전체: WHERE (class_name LIKE '%-P%' OR class_name LIKE '%-R%')
""",
))

SQL.append((
"""- 결과가 너무 많아지지 않도록 필요하면 LIMIT 20 을 붙인다.
- 상품 비교 질의는 사람이 읽을 수 있도록 product_name 을 반드시 포함한다.""",
"""- 목록을 요구하는 질의는 LIMIT 을 30 이상으로 둔다. 조건에 맞는 상품을 빠뜨리면
  오답이 된다. 최소/최대 하나만 묻는 질의는 LIMIT 5 로 충분하다.
- 최저/최고/가장 싼/가장 비싼 을 묻는 질의는 반드시 ORDER BY 를 넣는다.
- 상품 비교 질의는 사람이 읽을 수 있도록 product_name 을 반드시 포함한다.
- SELECT 에 class_name 과 total_fee 를 기본으로 포함한다.""",
))

# 실행 후 안전장치: 파이썬 재정렬 + 클래스 의미 주입
SQL.append((
'''class PensionSQLAgent:''',
'''# ──────────────────────────────────────────────────────────────────────
# 실행 후 안전장치
#
# LLM 이 만든 SQL 의 ORDER BY 를 믿지 않는다. "가장 싼 게 뭔가요?" 질의에서
# ORDER BY 가 빠지거나 클래스 필터가 좁아 최솟값을 놓친 사례가 실제로 있었다
# (평가 Q-014: 0.155% 상품을 두고 0.26% 를 "가장 낮다"고 답함).
# 숫자 컬럼이 있고 질의에 최저/최고 신호가 있으면 파이썬에서 다시 정렬한다.
# ──────────────────────────────────────────────────────────────────────

SORT_ASC_WORDS = ['가장 싼', '가장 낮은', '가장 저렴', '가장 적은', '제일 싼',
                  '제일 낮은', '제일 저렴', '최저', '싼 순', '낮은 순', '저렴한 순']
SORT_DESC_WORDS = ['가장 비싼', '가장 높은', '제일 비싼', '제일 높은',
                   '최고', '비싼 순', '높은 순']
FEE_COLS = ('total_fee', 'fee_total', 'total_cost')

CLASS_RULES = [
    ('P2', '퇴직연금'), ('-R', '퇴직연금'), ('-P', '연금저축(개인연금)'),
]


def describe_class(code):
    """class_desc 가 깨져 있으므로 class_name 코드에서 의미를 유도한다."""
    if not code:
        return None
    c = str(code).upper().replace(' ', '')
    account = None
    if 'P2' in c:
        account = '퇴직연금'
    elif c.startswith('C-R') or c.startswith('R') or '-R' in c:
        account = '퇴직연금'
    elif '-P' in c or c.endswith('P'):
        account = '연금저축(개인연금)'
    if account is None:
        return None
    if c.startswith('S'):
        channel = '온라인슈퍼'
    elif c.endswith('E'):
        channel = '온라인'
    else:
        channel = '오프라인'
    fee = '수수료선취' if c.startswith('A') else '수수료미징구'
    return f"{fee}-{channel}-{account}"


def _postprocess(query, cols, rows):
    """최저/최고 질의는 재정렬하고, class_name 이 있으면 의미 컬럼을 덧붙인다."""
    if not rows or not cols:
        return cols, rows
    rows = [list(r) for r in rows]

    fee_idx = next((cols.index(c) for c in FEE_COLS if c in cols), None)
    if fee_idx is not None:
        q = query.lower()
        if any(w in q for w in SORT_ASC_WORDS):
            rows.sort(key=lambda r: (r[fee_idx] is None, r[fee_idx]))
        elif any(w in q for w in SORT_DESC_WORDS):
            rows.sort(key=lambda r: (r[fee_idx] is None,
                                     -(r[fee_idx] if r[fee_idx] is not None else 0)))

    if 'class_name' in cols and 'class_meaning' not in cols:
        ci = cols.index('class_name')
        cols = list(cols) + ['class_meaning']
        for r in rows:
            r.append(describe_class(r[ci]))
    return cols, rows


class PensionSQLAgent:''',
))

SQL.append((
"""            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.close()
            return {"sql": sql, "columns": cols, "data": rows}""",
"""            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.close()
            cols, rows = _postprocess(query, cols, rows)
            return {"sql": sql, "columns": cols, "data": rows}""",
))


# ══════════════════════════════════════════════════════════════════════
# main.py — 통째로 교체 (예외 처리 + 규격 보장)
# ══════════════════════════════════════════════════════════════════════

MAIN_NEW = '''"""공모전 평가 API 서버.

    GET /answer?question_id={id}&question={질의}
    -> {question_id, question, retrieved_context, think_trace, answer}  (전부 문자열)

로컬 기동:
    python -m uvicorn main:app --host 127.0.0.1 --port 8000
"""
import sys
import time
import traceback

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from orchestrator import PensionOrchestrator

app = FastAPI(title="Mirae Asset AI Festival 2026 - Pension Agent API")
orchestrator = PensionOrchestrator()

# 주최측 타임아웃은 300초. 여유를 두고 이 시간을 넘기면 있는 것만으로 응답한다.
DEADLINE = 240


@app.get("/answer")
def get_answer(
    question_id: str = Query(..., description="평가 질의 ID"),
    question: str = Query(..., description="평가 질의 원문")
):
    t0 = time.monotonic()
    try:
        result = orchestrator.process(question)
        answer = str(result.get("answer") or "")
        if not answer.strip():
            answer = ("제공된 자료에서 관련 근거를 찾지 못했습니다. "
                      "질문을 조금 더 구체적으로 알려주시면 확인 가능한 범위에서 "
                      "안내드리겠습니다.")
        payload = {
            "question_id": str(question_id),
            "question": str(question),
            "retrieved_context": str(result.get("retrieved_context") or ""),
            "think_trace": str(result.get("think_trace") or ""),
            "answer": answer,
        }
    except Exception as exc:
        # 예외가 그대로 새면 500 이 나가고 그 문항은 통째로 0점이 된다.
        # 규격에 맞는 200 응답이 재시도 낭비도 적고 안전하다.
        print(f"[server] /answer 예외: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        payload = {
            "question_id": str(question_id),
            "question": str(question),
            "retrieved_context": "",
            "think_trace": f"처리 중 오류 발생: {type(exc).__name__}",
            "answer": ("죄송합니다. 답변 생성 중 오류가 발생했습니다. "
                       "잠시 후 다시 시도해 주세요."),
        }

    elapsed = time.monotonic() - t0
    print(f"[server] {question_id} {elapsed:.1f}초", file=sys.stderr)
    if elapsed > DEADLINE:
        print(f"[server] 경고: {elapsed:.0f}초 — 300초 한도에 근접", file=sys.stderr)
    return JSONResponse(content=payload, media_type="application/json")


@app.get("/health")
def health():
    """생존 확인용. 평가 규격에는 없는 엔드포인트."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
'''


# ══════════════════════════════════════════════════════════════════════

PATCHES = {
    "rag_agent.py": RAG,
    "orchestrator.py": ORC,
    "sql_agent.py": SQL,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="확인만, 쓰지 않음")
    ap.add_argument("--revert", action="store_true", help="백업으로 되돌리기")
    ap.add_argument("--dir", default=".", help="대상 폴더")
    a = ap.parse_args()
    root = Path(a.dir)

    if a.revert:
        n = 0
        for name in list(PATCHES) + ["main.py"]:
            bak = root / (name + BAK)
            if bak.exists():
                shutil.copy2(bak, root / name)
                print(f"  되돌림: {name}")
                n += 1
        print(f"\n{n}개 파일을 백업으로 되돌렸습니다.")
        return 0

    staged: dict[str, str] = {}
    problems: list[str] = []

    for name, patches in PATCHES.items():
        p = root / name
        if not p.exists():
            problems.append(f"{name}: 파일이 없습니다")
            continue
        text = p.read_text(encoding="utf-8")
        for i, (old, new) in enumerate(patches, 1):
            cnt = text.count(old)
            if cnt == 0:
                if new in text:
                    print(f"  - {name} #{i}: 이미 적용됨, 건너뜀")
                    continue
                problems.append(f"{name} #{i}: 대상 문자열을 찾지 못함\n"
                                f"      찾던 것: {old.strip().splitlines()[0][:70]}...")
                continue
            if cnt > 1:
                problems.append(f"{name} #{i}: {cnt}곳에서 발견(1곳이어야 함)")
                continue
            text = text.replace(old, new, 1)
            print(f"  + {name} #{i}: 적용")
        staged[name] = text

    staged["main.py"] = MAIN_NEW
    print("  + main.py: 전체 교체 (예외 처리 + 규격 보장)")

    if problems:
        print("\n" + "!" * 62)
        print("적용을 중단합니다. 아무 파일도 수정하지 않았습니다.")
        print("!" * 62)
        for p in problems:
            print(f"   . {p}")
        return 1

    # 문법 검사
    import ast
    for name, text in staged.items():
        try:
            ast.parse(text)
        except SyntaxError as e:
            print(f"\n[문법 오류] {name}:{e.lineno} {e.msg}")
            print("아무 파일도 수정하지 않았습니다.")
            return 1
    print("\n  문법 검사 통과")

    if a.check:
        print("  --check 모드라 쓰지 않았습니다.")
        return 0

    for name, text in staged.items():
        p = root / name
        bak = root / (name + BAK)
        if p.exists() and not bak.exists():
            shutil.copy2(p, bak)
        p.write_text(text, encoding="utf-8")
    print(f"\n  {len(staged)}개 파일 적용 완료. 원본은 *{BAK} 로 백업했습니다.")
    print("  되돌리려면: python apply_fixes.py --revert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
