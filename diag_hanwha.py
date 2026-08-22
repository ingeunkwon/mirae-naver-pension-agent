# -*- coding: utf-8 -*-
"""fund_prospectus_v2.sqlite 를 직접 열어서 '한화' 펀드가 실제로 어떻게
저장돼 있는지 확인한다. LLM 호출도, 서버 실행도 필요 없다 — DB 파일만
있으면 된다.

확인할 것
---------
1) product_name 에 '한화'가 들어간 상품이 fund_products 에 실제로 있는가.
2) 있다면 그 상품의 product_type 이 정확히 '채권형' 문자열인가
   (앞뒤 공백/다른 표기일 가능성 확인).
3) 그 상품의 fund_class_fees 클래스별 total_fee 는 얼마인가 — 0.207이
   있는가.
4) product_type 컬럼에 실제로 어떤 값들이 들어있는지 전체 목록도 같이 뽑는다
   (혹시 '채권형 ' 처럼 공백이 섞였거나 다른 라벨을 쓰는지 확인).

사용법: python diag_hanwha.py  ->  diag_hanwha.json 에 저장 + 화면 출력
"""
import json
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "data" / "fund_prospectus_v2.sqlite"


def main():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    out = {}

    # 1) '한화' 상품 검색
    cur.execute(
        "SELECT product_code, product_name, asset_manager, product_type "
        "FROM fund_products WHERE product_name LIKE '%한화%'"
    )
    hanwha_products = cur.fetchall()
    out["hanwha_products"] = hanwha_products

    # 2) 그 상품들의 클래스별 보수
    hanwha_fees = []
    for code, name, mgr, ptype in hanwha_products:
        cur.execute(
            "SELECT class_name, class_desc, total_fee "
            "FROM fund_class_fees WHERE product_code = ?",
            (code,),
        )
        for cn, cd, fee in cur.fetchall():
            hanwha_fees.append({
                "product_code": code, "product_name": name,
                "product_type": ptype, "class_name": cn,
                "class_desc": cd, "total_fee": fee,
            })
    out["hanwha_class_fees"] = hanwha_fees

    # 3) product_type 컬럼에 실제로 어떤 값들이 있는지 (repr로 공백까지 보이게)
    cur.execute("SELECT DISTINCT product_type FROM fund_products")
    types = [r[0] for r in cur.fetchall()]
    out["distinct_product_types"] = [repr(t) for t in types]

    # 4) 참고: Q-017이 쓴 조건과 완전히 동일하게, 상품명 조건 없이
    #    '채권형' + class LIKE '%-P%' + total_fee<=0.3 인 전체 개수도 같이 센다
    cur.execute(
        "SELECT COUNT(*) FROM fund_products fp "
        "JOIN fund_class_fees fc ON fp.product_code = fc.product_code "
        "WHERE fp.product_type = '채권형' AND fc.class_name LIKE '%-P%' "
        "AND fc.total_fee <= 0.3"
    )
    out["q017_exact_match_count"] = cur.fetchone()[0]

    conn.close()

    (BASE / "diag_hanwha.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 한화 상품 ===")
    for row in hanwha_products:
        print(" ", row)
    print()
    print("=== 한화 클래스별 보수 ===")
    for row in hanwha_fees:
        print(" ", row)
    print()
    print("=== product_type 전체 값 목록 ===")
    for t in types:
        print(" ", repr(t))
    print()
    print("saved diag_hanwha.json")


if __name__ == "__main__":
    main()
