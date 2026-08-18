import json
import os
import re
import sqlite3

# ============================================================
# 0. 경로 설정
# ============================================================
# 현재 폴더에 위치한 JSON 파일
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_INPUT_PATH = os.path.join(BASE_DIR, "knowledge_base_final.json")
if not os.path.exists(JSON_INPUT_PATH):
  JSON_INPUT_PATH = os.path.join(BASE_DIR, "TalkFile_knowledge_base_final.json")

DB_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DB_DIR, "financial_data.sqlite")
os.makedirs(DB_DIR, exist_ok=True)


# ============================================================
# 1. 텍스트 수치 정규화 유틸리티 함수
# ============================================================
def parse_korean_currency(text: str) -> int:
  """'900만 원', '1,200만 원', '148만 5천 원' -> 정수 원 단위로 변환"""
  if not text:
    return 0
  text = text.replace(",", "").replace(" ", "")
  total = 0

  uk_match = re.search(r"(\d+)억", text)
  if uk_match:
    total += int(uk_match.group(1)) * 100_000_000

  man_match = re.search(r"(\d+)만", text)
  if man_match:
    total += int(man_match.group(1)) * 10_000

  cheon_match = re.search(r"(\d+)천", text)
  if cheon_match:
    total += int(cheon_match.group(1)) * 1_000

  if total == 0:
    digits = re.findall(r"\d+", text)
    if digits:
      total = int("".join(digits))

  return total


def parse_percentage(text: str) -> float:
  """'16.5%', '13.2%', '5.5%' -> 0.165, 0.132 등 소수점으로 변환"""
  if not text:
    return 0.0
  match = re.search(r"(\d+\.?\d*)\s*%", text)
  if match:
    return round(float(match.group(1)) / 100.0, 5)
  return 0.0


# ============================================================
# 2. SQLite 테이블 초기화 (테이블 생성 및 기존 데이터 리셋)
# ============================================================
def init_database(conn):
  cursor = conn.cursor()

  # 1) 세액공제 한도표
  cursor.execute("""
    CREATE TABLE IF NOT EXISTS pension_tax_limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        income_tier TEXT,
        deduction_rate REAL,
        pension_limit INTEGER,
        isa_additional_limit INTEGER,
        total_max_deposit INTEGER,
        total_max_benefit INTEGER
    )
    """)

  # 2) 연령별 연금소득세율표
  cursor.execute("""
    CREATE TABLE IF NOT EXISTS pension_age_tax_rates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        age_category TEXT,
        min_age INTEGER,
        max_age INTEGER,
        tax_rate REAL,
        tax_type TEXT
    )
    """)

  # 3) 압류 금지 규칙표
  cursor.execute("""
    CREATE TABLE IF NOT EXISTS pension_seizure_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_type TEXT,
        is_seizable TEXT,
        seizure_ratio REAL,
        legal_basis TEXT
    )
    """)

  # 4) [추가] IRP 중도인출 법정 사유 및 과세율 정형 테이블
  cursor.execute("""
    CREATE TABLE IF NOT EXISTS pension_withdrawal_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reason TEXT,
        is_statutory_allowed INTEGER,
        is_tax_exempt_reason INTEGER,
        tax_rate_label TEXT,
        tax_rate REAL
    )
    """)

  # 5) 문서 내 모든 원본 표 아카이브
  cursor.execute("""
    CREATE TABLE IF NOT EXISTS document_tables (
        record_id TEXT PRIMARY KEY,
        document TEXT,
        file_name TEXT,
        page INTEGER,
        table_title TEXT,
        headers_text TEXT,
        table_text TEXT
    )
    """)

  # 기존 데이터 리셋 (중복 방지)
  cursor.execute("DELETE FROM pension_tax_limits")
  cursor.execute("DELETE FROM pension_age_tax_rates")
  cursor.execute("DELETE FROM pension_seizure_rules")
  cursor.execute("DELETE FROM pension_withdrawal_rules")
  cursor.execute("DELETE FROM document_tables")

  conn.commit()
  print("🛠️ [1/4] SQLite 테이블 초기화 완료")


# ============================================================
# 3. JSON 데이터 파싱 및 테이블별 적재
# ============================================================
def populate_sql_database():
  if not os.path.exists(JSON_INPUT_PATH):
    print(f"❌ JSON 파일을 찾을 수 없습니다: {JSON_INPUT_PATH}")
    return

  print(f"📖 [2/4] JSON 데이터 로드 중: {JSON_INPUT_PATH}")
  with open(JSON_INPUT_PATH, "r", encoding="utf-8") as f:
    records = json.load(f)

  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  init_database(conn)

  table_count = 0
  print("⚙️ [3/4] 데이터 파싱 및 SQL 테이블 적재 시작...")

  for item in records:
    content_type = item.get("content_type", "")
    record_id = item.get("record_id", "")
    doc_name = item.get("document", "")
    file_name = item.get("file_name", "")
    page = item.get("page") or item.get("start_page") or 1

    # A. 원본 표 전체 백업 (document_tables)
    if content_type == "table":
      table_count += 1
      table_title = item.get("table_title") or f"{doc_name}_표_{table_count}"
      headers = item.get("headers", [])
      headers_str = (
          " | ".join(headers) if isinstance(headers, list) else str(headers)
      )
      table_text = item.get("table_text") or item.get("text", "")

      cursor.execute(
          """
            INSERT OR REPLACE INTO document_tables (record_id, document, file_name, page, table_title, headers_text, table_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
          (
              record_id,
              doc_name,
              file_name,
              page,
              table_title,
              headers_str,
              table_text,
          ),
      )

    # B. 세액공제 납입한도 & ISA 추가공제 (doc23 table_00012)
    if record_id == "table_00012" or (
        doc_name == "doc23" and "합산 최대 공제" in str(item.get("table_text", ""))
    ):
      rows = item.get("rows", [])
      pension_limit = 9_000_000
      isa_limit = 3_000_000
      total_deposit = 12_000_000

      for row in rows:
        if len(row) >= 4:
          label = row[0]
          if "16.5%" in label:
            benefit_165 = parse_korean_currency(row[3])
            cursor.execute(
                """
                        INSERT INTO pension_tax_limits 
                        (income_tier, deduction_rate, pension_limit, isa_additional_limit, total_max_deposit, total_max_benefit)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                (
                    "총급여 5,500만원 이하 (종합소득 4,500만원 이하)",
                    0.165,
                    pension_limit,
                    isa_limit,
                    total_deposit,
                    benefit_165,
                ),
            )
          elif "13.2%" in label:
            benefit_132 = parse_korean_currency(row[3])
            cursor.execute(
                """
                        INSERT INTO pension_tax_limits 
                        (income_tier, deduction_rate, pension_limit, isa_additional_limit, total_max_deposit, total_max_benefit)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                (
                    "총급여 5,500만원 초과 (종합소득 4,500만원 초과)",
                    0.132,
                    pension_limit,
                    isa_limit,
                    total_deposit,
                    benefit_132,
                ),
            )

    # C. 연령대별 연금소득세율 (doc20 table_00011)
    if record_id == "table_00011" or (
        doc_name == "doc20"
        and "연금소득세 (연금수령)" in str(item.get("table_text", ""))
    ):
      age_rates = [
          (
              "69세 이하 (만 55세 ~ 만 69세)",
              55,
              69,
              0.055,
              "연금소득세(저율 분리과세)",
          ),
          ("70세 ~ 79세", 70, 79, 0.044, "연금소득세(저율 분리과세)"),
          ("80세 이상", 80, 999, 0.033, "연금소득세(저율 분리과세)"),
      ]
      for cat, min_a, max_a, rate, t_type in age_rates:
        cursor.execute(
            """
                INSERT INTO pension_age_tax_rates (age_category, min_age, max_age, tax_rate, tax_type)
                VALUES (?, ?, ?, ?, ?)
                """,
            (cat, min_a, max_a, rate, t_type),
        )

    # D. 퇴직급여 압류 가능 여부 요약표 (doc16 table_00008)
    if record_id == "table_00008" or (
        doc_name == "doc16"
        and "퇴직급여와 압류" in str(item.get("table_text", ""))
    ):
      rules = [
          (
              "퇴직연금(DB, DC, IRP)",
              "전액 압류 금지",
              0.0,
              "근로자퇴직급여 보장법 제7조제1항",
          ),
          ("임원의 퇴직연금", "1/2까지 압류 가능", 0.5, "민사집행법 제246조제1항제5호"),
          ("DC 추가납입금", "전액 압류 금지", 0.0, "근로자퇴직급여 보장법"),
          (
              "명예퇴직금, 경영성과급(규약 내 납입)",
              "전액 압류 금지",
              0.0,
              "근로자퇴직급여 보장법",
          ),
          ("미납된 부담금", "전액 압류 금지", 0.0, "근로자퇴직급여 보장법"),
          ("운용수익", "전액 압류 금지", 0.0, "근로자퇴직급여 보장법"),
          (
              "기존 퇴직금(일반급여계좌 수령분)",
              "1/2까지 압류 가능",
              0.5,
              "민사집행법 제246조",
          ),
          (
              "IRP로 의무 이전한 퇴직금",
              "전액 압류 금지",
              0.0,
              "근로자퇴직급여 보장법 (2022.4.14 이후)",
          ),
      ]
      for asset, seizable, ratio, basis in rules:
        cursor.execute(
            """
                INSERT INTO pension_seizure_rules (asset_type, is_seizable, seizure_ratio, legal_basis)
                VALUES (?, ?, ?, ?)
                """,
            (asset, seizable, ratio, basis),
        )

  # E. [추가] IRP 중도인출 사유 및 과세율 정형 데이터 적재
  withdrawal_rules = [
      ("무주택자인 가입자가 본인 명의로 주택 구입", 1, 0, "16.5% 기타소득세", 0.165),
      ("무주택자인 가입자의 주거 목적 전세보증금 부담", 1, 0, "16.5% 기타소득세", 0.165),
      ("가입자 또는 부양가족의 6개월 이상 요양", 1, 1, "5.5% ~ 3.3% 연금소득세", 0.055),
      ("가입자의 파산 선고 / 개인회생절차 개시", 1, 1, "5.5% ~ 3.3% 연금소득세", 0.055),
      (
          "천재지변으로 고용노동부장관이 정한 사유에 해당",
          1,
          1,
          "5.5% ~ 3.3% 연금소득세",
          0.055,
      ),
      ("퇴직연금 담보대출 원리금 상환", 1, 0, "16.5% 기타소득세", 0.165),
      ("가입자 사망 또는 해외이주", 0, 1, "5.5% ~ 3.3% 연금소득세", 0.055),
      ("금융회사의 영업정지 등", 0, 1, "5.5% ~ 3.3% 연금소득세", 0.055),
  ]
  cursor.executemany(
      """
    INSERT INTO pension_withdrawal_rules (reason, is_statutory_allowed, is_tax_exempt_reason, tax_rate_label, tax_rate)
    VALUES (?, ?, ?, ?, ?)
    """,
      withdrawal_rules,
  )

  conn.commit()
  conn.close()

  print(f"🎉 [4/4] SQLite 데이터베이스 구축 완료!")
  print(f"📁 생성된 DB 파일: {DB_PATH}")


if __name__ == "__main__":
  populate_sql_database()