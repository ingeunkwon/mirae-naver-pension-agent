import json
import sqlite3
from pathlib import Path
import requests
from rag_agent import build_headers, TIMEOUT, HCX_URL

BASE_DIR = Path(__file__).resolve().parent

class PensionSQLAgent:
    def __init__(
        self,
        fin_db_path=BASE_DIR / "data" / "financial_data.sqlite",
        fund_db_path=BASE_DIR / "data" / "fund_prospectus.sqlite"
    ):
        self.fin_db_path = str(fin_db_path)
        self.fund_db_path = str(fund_db_path)

    def generate_and_execute(self, query: str, db_type: str) -> dict:
        db_path = self.fin_db_path if db_type == "FIN" else self.fund_db_path
        
        schema_info = """
[financial_data.sqlite 테이블 스키마]
1. pension_tax_limits (income_tier TEXT, deduction_rate REAL, pension_limit INTEGER, isa_additional_limit INTEGER, total_max_deposit INTEGER, total_max_benefit INTEGER)
2. pension_age_tax_rates (age_category TEXT, min_age INTEGER, max_age INTEGER, tax_rate REAL, tax_type TEXT)
3. pension_seizure_rules (asset_type TEXT, is_seizable TEXT, seizure_ratio REAL, legal_basis TEXT)
4. pension_withdrawal_rules (reason TEXT, is_statutory_allowed INTEGER, is_tax_exempt_reason INTEGER, tax_rate_label TEXT, tax_rate REAL)
""" if db_type == "FIN" else """
[fund_prospectus.sqlite 테이블 스키마]
1. fund_products (product_code TEXT PRIMARY KEY, product_name TEXT, asset_manager TEXT, risk_grade INTEGER, risk_label TEXT, product_type TEXT)
2. fund_class_fees (product_code TEXT, class_name TEXT, class_desc TEXT, upfront_fee REAL, mgmt_fee REAL, sales_fee REAL, total_fee REAL)
3. fund_profiles (product_code TEXT, investment_objective TEXT, investment_strategy TEXT, investment_risk TEXT)
"""

        prompt = f"""너는 SQLite 쿼리 생성 전문가다. 아래 스키마를 바탕으로 사용자의 질문에 정확히 부합하는 단일 SELECT SQL문만 작성하라.

{schema_info}

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
            
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.close()
            return {"sql": sql, "columns": cols, "data": rows}
        except Exception as e:
            return {"error": str(e), "sql": None, "data": []}