import json
from pathlib import Path
import requests
from rag_agent import PensionRAGAgent, build_headers, TIMEOUT, HCX_URL
from sql_agent import PensionSQLAgent

BASE_DIR = Path(__file__).resolve().parent

class PensionOrchestrator:
    def __init__(self):
        self.rag_agent = PensionRAGAgent(
            embedding_file=BASE_DIR / "data" / "vector_db" / "rag_embeddings_v2.json"
        )
        self.sql_agent = PensionSQLAgent(
            fin_db_path=BASE_DIR / "data" / "financial_data.sqlite",
            fund_db_path=BASE_DIR / "data" / "fund_prospectus.sqlite"
        )

    def route_query(self, query: str) -> str:
        router_prompt = f"""사용자 질문을 분석하여 최적의 처리 방식을 결정하라. (재질문은 금지되며 완결된 답변을 해야 함)

[분류 기준]
1. SQL_FIN: 세액공제 한도액, 연령대별 연금소득세율, 압류 규정 등 제도 수치 조회
2. SQL_FUND: 펀드명, 위험등급(1~6등급), 클래스별 보수율(TER) 수치 비교 및 조건 검색
3. HYBRID: 제도 규정과 수치/상품 비교가 모두 필요한 복합 질문, 또는 조건이 누락되어 조건별 가정이 필요한 질의
4. RAG: 연금 제도 규칙, 가입 자격, 중도인출 법정 사유, 이전 절차 등 약관/규정 설명

질문: {query}

반드시 JSON만 출력: {{"route": "SQL_FIN" | "SQL_FUND" | "HYBRID" | "RAG"}}"""

        payload = {
            "messages": [
                {"role": "system", "content": "JSON 형식으로만 답하라."},
                {"role": "user", "content": router_prompt}
            ],
            "temperature": 0.0
        }
        try:
            res = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT).json()
            content = res.get("result", {}).get("message", {}).get("content", "").strip()
            if content.startswith("```"):
                content = content.replace("```json", "").replace("```", "").strip()
            return json.loads(content).get("route", "RAG")
        except Exception:
            return "RAG"

    def synthesize_answer(self, query: str, rag_context: str, sql_context: str) -> str:
        system_prompt = """너는 연금·퇴직연금 전문 AI 상담사다.
제공된 근거 자료만을 사용하여 질문에 답변하라.

[핵심 작성 원칙]
1. 사용자에게 추가 질문(역질문)을 절대로 하지 않는다.
2. 질문에 조건(소득 구간, 위험 성향, 계좌 종류 등)이 명시되지 않았다면, 가능한 모든 경우의 수를 항목별/조건별로 나누어 한 번에 완결되게 설명한다.
3. 근거 자료의 정확한 수치(금액, 세율, 보수율, 등급)를 그대로 인용한다."""

        user_prompt = f"""[사용자 질문]
{query}

[제도 및 약관 문서 근거]
{rag_context}

[정형 DB 조회 수치 근거]
{sql_context}

위 근거를 종합하여 역질문 없이 완결된 답변을 작성하라."""

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1,
            "maxCompletionTokens": 1000
        }
        res = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT).json()
        return res.get("result", {}).get("message", {}).get("content", "").strip()

    def process(self, query: str) -> dict:
        route = self.route_query(query)
        think_trace_list = [f"1. 의도 분류 및 라우팅: {route}"]
        
        rag_context = ""
        sql_context = ""
        retrieved_sources = []

        # 1. SQL 실행
        if route in ["SQL_FIN", "SQL_FUND", "HYBRID"]:
            db_type = "FIN" if route == "SQL_FIN" else "FUND"
            if route == "HYBRID":
                db_type = "FUND" if any(k in query for k in ["펀드", "상품", "보수", "등급", "추천"]) else "FIN"
            
            sql_res = self.sql_agent.generate_and_execute(query, db_type)
            sql_context = json.dumps(sql_res, ensure_ascii=False)
            think_trace_list.append(f"2. 정형 DB 조회 완료 (SQL: {sql_res.get('sql', 'N/A')})")
            retrieved_sources.append({"source_file": f"{db_type.lower()}_data.sqlite"})

        # 2. RAG 실행
        if route in ["RAG", "HYBRID"]:
            rag_res = self.rag_agent.ask(query)
            rag_context = rag_res.get("raw_context", "")
            for s in rag_res.get("sources", []):
                retrieved_sources.append(s)
            think_trace_list.append(f"3. 비정형 약관 RAG 검색 완료 ({len(rag_res.get('sources', []))}건 인용)")

        # 3. 답변 생성
        if route == "RAG" and rag_res.get("answer"):
            final_answer = rag_res.get("answer")
            think_trace_list.append("4. RAG 기반 직접 답변 완료")
        else:
            final_answer = self.synthesize_answer(query, rag_context, sql_context)
            think_trace_list.append("4. 정형/비정형 통합 분석 및 다각도 조건 분기 답변 완료")

        return {
            "answer": final_answer,
            "think_trace": " -> ".join(think_trace_list),
            "retrieved_context": f"{rag_context}\n{sql_context}".strip(),
            "sources": retrieved_sources
        }