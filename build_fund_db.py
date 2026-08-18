import json
import os
import re
import sqlite3

# ============================================================
# 1. 파일 및 폴더 경로 설정 (VS Code 디렉토리 구조 100% 일치)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 입력 파일: PYTHON 루트에 있는 2개 JSON 파일
PROFILES_JSON = os.path.join(BASE_DIR, "product_profiles.json")
SECTIONS_JSON = os.path.join(BASE_DIR, "product_sections.json")

# 출력 파일: PYTHON/processed/fund_prospectus.sqlite
OUTPUT_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(OUTPUT_DIR, "fund_prospectus.sqlite")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# 2. SQLite 데이터베이스 테이블 스키마 초기화
# ============================================================
def init_database(conn: sqlite3.Connection):
    cur = conn.cursor()

    # 1) 펀드 기본 메타데이터 (product_profiles.json 기반)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fund_products (
        product_code TEXT PRIMARY KEY,
        product_name TEXT NOT NULL,
        asset_manager TEXT,
        document_date TEXT,
        risk_grade INTEGER,
        risk_label TEXT,
        product_type TEXT,
        page_count INTEGER,
        source_file TEXT,
        source_path TEXT
    )
    """)

    # 2) 펀드 전문 섹션 통합 테이블 (7대 핵심 항목 원문)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fund_profiles (
        product_code TEXT PRIMARY KEY,
        investment_objective TEXT,
        investment_target TEXT,
        investment_strategy TEXT,
        investment_risk TEXT,
        purchase_redemption TEXT,
        fees TEXT,
        tax TEXT,
        FOREIGN KEY (product_code) REFERENCES fund_products(product_code)
    )
    """)

    # 3) 클래스별 정형 보수/수수료 테이블 (Text-to-SQL 수치 비교용)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fund_class_fees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT,
        class_name TEXT,
        class_desc TEXT,
        upfront_fee REAL,
        mgmt_fee REAL,
        sales_fee REAL,
        trust_fee REAL,
        admin_fee REAL,
        total_fee REAL,
        FOREIGN KEY (product_code) REFERENCES fund_products(product_code)
    )
    """)

    # 4) 청크 단위 섹션 아카이브 (product_sections.json 기반, RAG 검색용)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fund_sections (
        record_id TEXT PRIMARY KEY,
        product_code TEXT,
        product_name TEXT,
        section TEXT,
        chunk_no INTEGER,
        page_start INTEGER,
        page_end INTEGER,
        text TEXT,
        search_text TEXT,
        source_file TEXT,
        FOREIGN KEY (product_code) REFERENCES fund_products(product_code)
    )
    """)

    cur.execute("DELETE FROM fund_products")
    cur.execute("DELETE FROM fund_profiles")
    cur.execute("DELETE FROM fund_class_fees")
    cur.execute("DELETE FROM fund_sections")
    conn.commit()


# ============================================================
# 3. 보수 및 수수료 정규표현식 파서
# ============================================================
def parse_fee_breakdown(fees_text: str):
    """fees 텍스트 표에서 클래스별 운용/판매/신탁/사무관리/총보수율을 추출"""
    if not fees_text:
        return []

    patterns = [
        ("종류A", "수수료선취-오프라인", r"종류A\s+수수료선취-오프라인\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류A-e", "수수료선취-온라인", r"종류A-e\s+수수료선취-온라인\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C1", "수수료미징구-오프라인-보수체감", r"종류C1\s+수수료미징구-오프라인-보수체감\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C2", "수수료미징구-오프라인-보수체감", r"종류C2\s+수수료미징구-오프라인-보수체감\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C3", "수수료미징구-오프라인-보수체감", r"종류C3\s+수수료미징구-오프라인-보수체감\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C4", "수수료미징구-오프라인-보수체감", r"종류C4\s+수수료미징구-오프라인-보수체감\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C-e", "수수료미징구-온라인", r"종류C-e\s+수수료미징구-온라인(?:-개인연금)?\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C-P", "수수료미징구-오프라인-개인연금", r"종류C-P\s+수수료미징구-오프라인-개인연금\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C-Pe", "수수료미징구-온라인-개인연금", r"종류C-Pe\s+수수료미징구-온라인-개인연금\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C-P2", "수수료미징구-오프라인-퇴직연금", r"종류C-P2\s+수수료미징구-오프라인-퇴직연금\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C-P2e", "수수료미징구-온라인-퇴직연금", r"종류C-P2e\s+수수료미징구-온라인-퇴직연금\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류S", "수수료후취-온라인슈퍼", r"종류S\s+수수료후취-온라인슈퍼\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류S-P", "수수료미징구-온라인슈퍼-개인연금", r"종류S-P\s+수수료미징구-온라인슈퍼-개인연금\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C", "수수료미징구-오프라인-개인연금", r"종류C\s+수수료미징구-오프라인-개인연금\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류C-F", "수수료미징구-직판-개인연금", r"종류C-F\s+수수료미징구-직판[^\n]*\s+([\d\.]+)\s+([-\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류F", "수수료미징구-오프라인-랩", r"종류F\s+수수료미징구-오프라인-랩[^\n]*\s+([\d\.]+)\s+([-\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)"),
        ("종류CG", "수수료미징구-무권유저비용", r"종류CG\s+수수료미징구[^\n]*\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)")
    ]

    results = []
    for c_name, c_desc, pat in patterns:
        m = re.search(pat, fees_text)
        if m:
            mgmt = float(m.group(1))
            sales = float(m.group(2)) if m.group(2) != "-" else 0.0
            trust = float(m.group(3))
            admin = float(m.group(4))
            total = float(m.group(5))
            results.append({
                "class_name": c_name,
                "class_desc": c_desc,
                "upfront_fee": 0.0,
                "mgmt_fee": mgmt,
                "sales_fee": sales,
                "trust_fee": trust,
                "admin_fee": admin,
                "total_fee": total
            })
    return results


# ============================================================
# 4. DB 적재 실행 함수
# ============================================================
def main():
    print("=" * 65)
    print(f"🚀 SQLite DB 생성 시작: {DB_PATH}")
    print("=" * 65)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    init_database(conn)

    # --------------------------------------------------------
    # Step 1: product_profiles.json 적재
    # --------------------------------------------------------
    if os.path.exists(PROFILES_JSON):
        print(f"📄 [1/2] 펀드 프로필 데이터 적재 중: {PROFILES_JSON}")
        with open(PROFILES_JSON, "r", encoding="utf-8") as f:
            profiles = json.load(f)

        fund_count = 0
        fee_count = 0

        for item in profiles:
            p_code = item.get("product_code")
            if not p_code:
                continue

            # fund_products 테이블 저장
            cur.execute("""
            INSERT OR REPLACE INTO fund_products (
                product_code, product_name, asset_manager, document_date,
                risk_grade, risk_label, product_type, page_count, source_file, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p_code,
                item.get("product_name"),
                item.get("asset_manager"),
                item.get("document_date"),
                item.get("risk_grade"),
                item.get("risk_label"),
                item.get("product_type"),
                item.get("page_count", 0),
                item.get("source_file"),
                item.get("source_path")
            ))

            # fund_profiles 테이블 저장
            cur.execute("""
            INSERT OR REPLACE INTO fund_profiles (
                product_code, investment_objective, investment_target,
                investment_strategy, investment_risk, purchase_redemption, fees, tax
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p_code,
                item.get("investment_objective", ""),
                item.get("investment_target", ""),
                item.get("investment_strategy", ""),
                item.get("investment_risk", ""),
                item.get("purchase_redemption", ""),
                item.get("fees", ""),
                item.get("tax", "")
            ))

            # fund_class_fees 테이블 저장
            fees_text = item.get("fees", "")
            fee_list = parse_fee_breakdown(fees_text)
            for f_info in fee_list:
                cur.execute("""
                INSERT INTO fund_class_fees (
                    product_code, class_name, class_desc, upfront_fee,
                    mgmt_fee, sales_fee, trust_fee, admin_fee, total_fee
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    p_code,
                    f_info["class_name"],
                    f_info["class_desc"],
                    f_info["upfront_fee"],
                    f_info["mgmt_fee"],
                    f_info["sales_fee"],
                    f_info["trust_fee"],
                    f_info["admin_fee"],
                    f_info["total_fee"]
                ))
                fee_count += 1

            fund_count += 1

        print(f"  └─ ✅ 펀드 마스터 {fund_count}개 및 클래스 보수 데이터 {fee_count}건 등록 완료")
    else:
        print(f"  └─ ❌ 파일을 찾을 수 없습니다: {PROFILES_JSON}")

    # --------------------------------------------------------
    # Step 2: product_sections.json 적재
    # --------------------------------------------------------
    if os.path.exists(SECTIONS_JSON):
        print(f"📄 [2/2] 청크 섹션 데이터 적재 중: {SECTIONS_JSON}")
        with open(SECTIONS_JSON, "r", encoding="utf-8") as f:
            sections = json.load(f)

        sec_count = 0
        for item in sections:
            cur.execute("""
            INSERT OR REPLACE INTO fund_sections (
                record_id, product_code, product_name, section, chunk_no,
                page_start, page_end, text, search_text, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("record_id"),
                item.get("product_code"),
                item.get("product_name"),
                item.get("section"),
                item.get("chunk_no"),
                item.get("page_start"),
                item.get("page_end"),
                item.get("text"),
                item.get("search_text"),
                item.get("source_file")
            ))
            sec_count += 1

        print(f"  └─ ✅ 섹션 청크 레코드 {sec_count}건 등록 완료")
    else:
        print(f"  └─ ❌ 파일을 찾을 수 없습니다: {SECTIONS_JSON}")

    conn.commit()
    conn.close()

    print("=" * 65)
    print("🎉 SQLite DB 구축 성공!")
    print(f"📁 생성 위치: {DB_PATH}")
    print("=" * 65)


if __name__ == "__main__":
    main()