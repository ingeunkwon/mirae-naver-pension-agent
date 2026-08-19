import json
import sqlite3
from pathlib import Path
import requests
from rag_agent import build_headers, TIMEOUT, HCX_URL

BASE_DIR = Path(__file__).resolve().parent

FIN_SCHEMA = """
[financial_data.sqlite - 연금 제도/세제 정형 데이터]
1. pension_tax_limits (income_tier TEXT, deduction_rate REAL, pension_limit INTEGER,
     isa_additional_limit INTEGER, total_max_deposit INTEGER, total_max_benefit INTEGER)
   -- 소득 구간별 세액공제 한도. 금액 단위는 원.
2. pension_age_tax_rates (age_category TEXT, min_age INTEGER, max_age INTEGER,
     tax_rate REAL, tax_type TEXT)
   -- 연령대별 연금소득세율. 55~69세 5.5% / 70~79세 4.4% / 80세 이상 3.3%
3. pension_seizure_rules (asset_type TEXT, is_seizable TEXT, seizure_ratio REAL, legal_basis TEXT)
4. pension_withdrawal_rules (reason TEXT, is_statutory_allowed INTEGER,
     is_tax_exempt_reason INTEGER, tax_rate_label TEXT, tax_rate REAL)
5. document_tables (record_id TEXT, document TEXT, file_name TEXT, page INTEGER,
     table_title TEXT, table_text TEXT)
   -- 안내문에서 추출한 표 원문. 위 4개 표로 답할 수 없을 때 LIKE 검색에 사용.
"""

FUND_SCHEMA = """
[fund_prospectus_v2.sqlite - 펀드 투자설명서 정형 데이터 (100종)]

1. fund_products (product_code TEXT PK, product_name TEXT, asset_manager TEXT,
     document_date TEXT, risk_grade INTEGER, risk_label TEXT, product_type TEXT,
     master_fund_id TEXT, source_file TEXT)
   -- risk_grade 는 국내 표준 표기다. 숫자가 작을수록 위험하다.
      1=매우 높은 위험, 2=높은 위험, 3=다소 높은 위험,
      4=보통 위험, 5=낮은 위험, 6=매우 낮은 위험
      따라서 "안정적인/보수적인 상품"은 risk_grade >= 5,
             "공격적인/고위험 상품"은 risk_grade <= 2 로 조회한다.
   -- product_type: 채권형 / 주식형 / 주식-파생형 / 재간접형 / MMF 등
   -- master_fund_id: 같은 투자설명서를 공유하는 클래스 묶음. 상품 비교 시
      GROUP BY master_fund_id 로 중복 나열을 피할 수 있다.

2. fund_class_fees (product_code TEXT, class_name TEXT, class_desc TEXT,
     mgmt_fee REAL, sales_fee REAL, trust_fee REAL, admin_fee REAL,
     total_fee REAL, etc_cost REAL, total_cost REAL)
   -- 판매 클래스별 보수. 단위는 연 %. total_fee 가 총보수(TER).
      class_name 예: A(수수료선취-오프라인), A-E(온라인), C(수수료미징구-오프라인),
      C-P(개인연금), C-PE(온라인 개인연금), C-P2/C-R(퇴직연금), S(온라인슈퍼)
      연금계좌 질문이면 C-P / C-PE / C-P2 / C-R 계열을 우선 본다.

3. fund_performance (product_code TEXT, kind TEXT, class_name TEXT, period TEXT, value REAL)
   -- 연평균 수익률(%). kind: fund(펀드 전체) / class(클래스별) / benchmark(비교지수)
      / volatility(수익률 변동성).  period: 1y, 2y, 3y, 5y, since(설정이후)

4. fund_aum (product_code TEXT, class_name TEXT, end_balance_million INTEGER)
   -- 최근 회계기간말 잔고. 단위는 백만원(원문이 억원/천원이어도 백만원으로 환산 저장).
      시장잔고(설정규모) 판단에 사용한다.
      class_name 이 '_FUND' 이면 클래스 구분 없는 펀드 전체 합계다.
      투자설명서에 설정.환매현황이 없는 신규 펀드는 이 표에 행이 없다(약 절반).
      따라서 잔고를 물으면 LEFT JOIN 으로 조회하고, 값이 없으면 "설정 규모는
      제공된 투자설명서에 기재되어 있지 않습니다" 라고 한계를 밝힌다.

5. fund_profiles (product_code TEXT PK, investment_objective, investment_target,
     investment_strategy, investment_risk, purchase_redemption, valuation,
     fees, tax, financials, subscription_history, performance)
   -- 투자설명서 섹션 원문. 정성 질의는 LIKE 검색.

6. fund_sections (record_id TEXT PK, product_code TEXT, product_name TEXT,
     section TEXT, chunk_no INTEGER, text TEXT, search_text TEXT)
   -- 위 섹션을 청크로 나눈 것. 특정 문구를 찾을 때 search_text LIKE 로 검색.
"""


class PensionSQLAgent:
    def __init__(
        self,
        fin_db_path=BASE_DIR / "data" / "financial_data.sqlite",
        fund_db_path=BASE_DIR / "data" / "fund_prospectus_v2.sqlite"
    ):
        self.fin_db_path = str(fin_db_path)
        self.fund_db_path = str(fund_db_path)

    def generate_and_execute(self, query: str, db_type: str) -> dict:
        db_path = self.fin_db_path if db_type == "FIN" else self.fund_db_path
        schema_info = FIN_SCHEMA if db_type == "FIN" else FUND_SCHEMA

        prompt = f"""너는 SQLite 쿼리 생성 전문가다. 아래 스키마를 바탕으로 사용자의 질문에 정확히 부합하는 단일 SELECT SQL문만 작성하라.

{schema_info}

[작성 규칙]
- SELECT 문 하나만 작성한다. 수정/삭제 구문은 절대 쓰지 않는다.
- 결과가 너무 많아지지 않도록 필요하면 LIMIT 20 을 붙인다.
- 상품 비교 질의는 사람이 읽을 수 있도록 product_name 을 반드시 포함한다.

[질문]: {query}

반드시 순수 JSON 포맷으로만 출력하라:
{{"sql": "SELECT ...;"}}"""

        payload = {
            "messages": [
                {"role": "system", "content": "너는 정확한 SQL 쿼리 생성기다. JSON만 출력한다."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0
        }

        try:
            res = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT).json()
            content = res.get("result", {}).get("message", {}).get("content", "").strip()
            if content.startswith("```"):
                content = content.replace("```json", "").replace("```", "").strip()
            sql = json.loads(content).get("sql")
            if not sql or not sql.strip().lower().startswith("select"):
                return {"error": "SELECT 문이 아님", "sql": sql, "columns": [], "data": []}

            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.close()
            return {"sql": sql, "columns": cols, "data": rows}
        except Exception as e:
            return {"error": str(e), "sql": None, "columns": [], "data": []}
