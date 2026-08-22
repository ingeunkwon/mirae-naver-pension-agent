# -*- coding: utf-8 -*-
"""data/pension_rules.db 의 정형 팩트(pension_facts / fact_conditions)에 대한
자동 정합성 검사기.

팀원(Kwonjunil) 저장소의 validate_facts.py를 실제로 갖고 있지 않아(우리 쪽에
받은 건 완성된 pension_rules.db와 임베딩 파일뿐, 그 검사기 코드 자체는 아직
없음) 문자 그대로 포팅한 건 아니다. 팀원이 문서(구현방식_비교분석_...)에서
설명한 검사 목적 — 구간 겹침, 조건 자기모순, 수치-원문 불일치, 부정문 오분류
위험 — 을 우리 스키마(pension_facts/fact_conditions)에 맞춰 다시 짠 것이다.
받은 자료가 부족해서가 아니라 검사기 코드 자체를 못 받아서 새로 쓴 것.

검사기 자체도 신뢰할 수 없으면 있으나 마나다 — 아래 각 검사는 self_test()로
결함 있는 가짜 행을 일부러 넣어 실제로 걸리는지, 정상 데이터엔 조용한지
확인한 뒤에만 본검사를 돌린다 (팀원 문서에 나온 원칙을 그대로 따름).

사용법:
    python validate_facts.py                    # data/pension_rules.db 검사
    python validate_facts.py --db 다른경로.db     # 다른 DB 검사
결과: validate_facts_result.json 저장 + 화면에 요약 출력
"""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEFAULT_DB = BASE / "data" / "pension_rules.db"

# 부정문 오분류 — 세 팀 전부에서 실제로 터진 P0급 버그 계열
# ("이연퇴직소득이 없는데", "비거주자는", "안 하는데" 가 반대로 해석됨).
# 완전 자동 판정은 못 하므로 "이 조합은 사람이 한 번 더 봐야 한다"는
# 후보만 걸러낸다.
_NEGATION_RE = re.compile(
    r"없는|아닌|아니면|안\s|미충족|비거주자|해당하지\s*않|제외|불가능|못\s*(받|한다)"
)

_NUM_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def _load_facts(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT fact_id, rule_group_id, pension_type, category, item, "
        "condition_text, value_op, value_num, value_num_max, value_unit, "
        "value_text, value_bool, fact_role, valid_from, valid_to, "
        "source_file, quote FROM pension_facts"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_conditions(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT condition_id, fact_id, condition_key, condition_role, "
        "condition_token FROM fact_conditions"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ----------------------------------------------------------------- 검사 본체
def check_e0_range_invalid(facts):
    """E0: value_num_max 가 있는데 value_num 보다 작다 (구간이 뒤집힘)."""
    out = []
    for f in facts:
        a, b = f.get("value_num"), f.get("value_num_max")
        if a is not None and b is not None and b < a:
            out.append({"fact_id": f["fact_id"], "item": f["item"],
                        "value_num": a, "value_num_max": b})
    return out


def check_e1_scale_outlier(facts):
    """E1: 단위 대비 값이 비상식적으로 크다/작다.
    (%면 0~100 범위 밖, 원 단위면 100억 초과 — 이번 오타 건("148만 5천만원")
    처럼 스케일이 통째로 어긋나는 유형을 겨냥한 검사)
    """
    out = []
    for f in facts:
        v, unit = f.get("value_num"), (f.get("value_unit") or "")
        if v is None:
            continue
        if "%" in unit and not (0 <= v <= 100):
            out.append({"fact_id": f["fact_id"], "item": f["item"],
                        "value_num": v, "value_unit": unit, "reason": "퍼센트 범위 밖"})
        if "원" in unit and abs(v) > 1e10:
            out.append({"fact_id": f["fact_id"], "item": f["item"],
                        "value_num": v, "value_unit": unit, "reason": "금액이 100억 초과"})
    return out


def check_e2_quote_number_mismatch(facts):
    """E2: value_num이 원문(quote)에 숫자 그대로 안 보인다 — 다른 문장의
    수치를 잘못 집었거나 자릿수를 잘못 옮겼을 가능성."""
    out = []
    for f in facts:
        v, quote = f.get("value_num"), f.get("quote") or ""
        if v is None or not quote:
            continue
        # value_num을 "148.5"/"1485000"/"16.5" 형태로 원문에서 찾아본다.
        # 쉼표·공백을 뺀 원문 숫자열과 비교한다.
        target_candidates = {str(v), str(int(v)) if float(v).is_integer() else str(v)}
        quote_nums = {m.group(0).replace(",", "") for m in _NUM_RE.finditer(quote)}
        # quote_nums 안의 숫자를 이어붙이거나 나눠 만든 값도 일부 허용해야
        # (예: "148만 5천" -> 148, 5) 오탐이 너무 많아지므로, 최소한
        # value_num의 정수부가 quote 숫자 목록 어딘가에 나타나는지만 본다.
        int_part = str(int(v)) if v == int(v) else None
        found = any(t in quote_nums for t in target_candidates)
        if int_part:
            found = found or any(int_part in n or n in int_part for n in quote_nums if n)
        if not found:
            out.append({"fact_id": f["fact_id"], "item": f["item"],
                        "value_num": v, "quote_head": quote[:80]})
    return out


def check_e3_contradictory_duplicate(facts):
    """E3: 같은 항목(item+pension_type+source_file)인데 value_op='='이고
    값이 서로 다른 행이 둘 이상 — 조건 없이 같은 사실을 다르게 말하고 있으면
    둘 중 하나는 오추출이거나, 조건이 빠져서 다른 값처럼 보이는 것이다."""
    groups: dict[tuple, list] = {}
    for f in facts:
        if f.get("value_op") != "=" or f.get("value_num") is None:
            continue
        key = (f.get("item"), f.get("pension_type"), f.get("source_file"))
        groups.setdefault(key, []).append(f)
    out = []
    for key, rows in groups.items():
        vals = {r["value_num"] for r in rows}
        if len(vals) > 1:
            out.append({
                "item": key[0], "pension_type": key[1], "source_file": key[2],
                "fact_ids": [r["fact_id"] for r in rows],
                "values": sorted(vals),
            })
    return out


def check_e4_date_range_invalid(facts):
    """E4: valid_from > valid_to (유효기간이 뒤집힘)."""
    out = []
    for f in facts:
        a, b = f.get("valid_from"), f.get("valid_to")
        if a and b and str(a) > str(b):
            out.append({"fact_id": f["fact_id"], "item": f["item"],
                        "valid_from": a, "valid_to": b})
    return out


def check_e5_missing_provenance(facts):
    """E5: 근거 추적이 끊긴 행 — quote/source_file/fact_id 중 하나라도 비었다.
    (팀원 설계 원칙: "모든 근거가 record_id -> chunk_id -> source_file p.N 을 갖는다")
    """
    out = []
    for f in facts:
        missing = [k for k in ("fact_id", "source_file", "quote") if not f.get(k)]
        if missing:
            out.append({"fact_id": f.get("fact_id"), "item": f.get("item"),
                        "missing": missing})
    return out


def check_e6_negation_risk(facts):
    """E6: 부정문 오분류 위험 후보 — condition_text/quote에 부정어가 있는데
    value_bool이 True로 잡혀 있는 조합. 자동으로 틀렸다고 단정하지 않고
    "사람이 한 번 더 봐야 하는 후보"만 뽑는다 (세 팀 공통으로 실제 발생한
    버그 계열이라 확정 오류가 아니라 감사용 후보 리스트로 남긴다)."""
    out = []
    for f in facts:
        if f.get("value_bool") != 1:
            continue
        text = f"{f.get('condition_text') or ''} {f.get('quote') or ''}"
        if _NEGATION_RE.search(text):
            out.append({"fact_id": f["fact_id"], "item": f["item"],
                        "condition_text": f.get("condition_text"),
                        "quote_head": (f.get("quote") or "")[:80]})
    return out


def check_e7_condition_role_invalid(conditions):
    """E7: fact_conditions.condition_role 이 selector/requirement 둘 중
    하나가 아니다 — 이 값이 깨지면 "요건 미충족 자체가 답"인 fact(requirement)가
    엉뚱하게 필터링돼 근거가 통째로 사라질 수 있다 (팀원 문서의 핵심 사례:
    "비거주자는 IRP 가입할 수 있어?")."""
    out = []
    for c in conditions:
        if c.get("condition_role") not in ("selector", "requirement"):
            out.append({"condition_id": c["condition_id"], "fact_id": c["fact_id"],
                        "condition_role": c.get("condition_role")})
    return out


CHECKS_ON_FACTS = [
    ("E0_RANGE_INVALID", check_e0_range_invalid),
    ("E1_SCALE_OUTLIER", check_e1_scale_outlier),
    ("E2_QUOTE_NUMBER_MISMATCH", check_e2_quote_number_mismatch),
    ("E3_CONTRADICTORY_DUPLICATE", check_e3_contradictory_duplicate),
    ("E4_DATE_RANGE_INVALID", check_e4_date_range_invalid),
    ("E5_MISSING_PROVENANCE", check_e5_missing_provenance),
    ("E6_NEGATION_RISK", check_e6_negation_risk),
]


# --------------------------------------------------------------- self_test
def self_test():
    """검사기 자체가 살아있는지 확인 — 결함 있는 가짜 행을 넣어서 걸리는지,
    정상 행엔 조용한지 본다. 한 번도 안 걸리는 검사기는 있으나 마나다."""
    good = [{
        "fact_id": "f_ok", "item": "공제율", "pension_type": "IRP",
        "condition_text": "총급여 5,500만원 이하", "value_op": "=",
        "value_num": 16.5, "value_num_max": None, "value_unit": "%",
        "value_bool": None, "fact_role": "requirement",
        "valid_from": "2020-01-01", "valid_to": None,
        "source_file": "doc27.pdf", "quote": "공제율 16.5% 적용",
    }]
    bad = [
        {**good[0], "fact_id": "f_bad_e0", "value_num": 50, "value_num_max": 10},
        {**good[0], "fact_id": "f_bad_e1", "value_num": 999, "value_unit": "%"},
        {**good[0], "fact_id": "f_bad_e2", "value_num": 77.7, "quote": "전혀 다른 숫자 3.3% 이야기"},
        {**good[0], "fact_id": "f_bad_e4", "valid_from": "2025-01-01", "valid_to": "2020-01-01"},
        {**good[0], "fact_id": "f_bad_e5", "source_file": None, "quote": ""},
        {**good[0], "fact_id": "f_bad_e6", "value_bool": 1,
         "condition_text": "비거주자는 해당하지 않음", "quote": "비거주자는 안 된다"},
    ]
    e3_pair = [
        {**good[0], "fact_id": "f_dup1", "value_num": 16.5, "source_file": "docX"},
        {**good[0], "fact_id": "f_dup2", "value_num": 13.2, "source_file": "docX"},
    ]

    problems = []
    # 정상 데이터엔 조용해야 한다
    for name, fn in CHECKS_ON_FACTS:
        if name == "E3_CONTRADICTORY_DUPLICATE":
            continue
        res = fn(good)
        if res:
            problems.append(f"{name}: 정상 데이터인데 오탐 발생 -> {res}")

    # 결함 데이터엔 걸려야 한다
    single_checks = {
        "E0_RANGE_INVALID": [bad[0]],
        "E1_SCALE_OUTLIER": [bad[1]],
        "E2_QUOTE_NUMBER_MISMATCH": [bad[2]],
        "E4_DATE_RANGE_INVALID": [bad[3]],
        "E5_MISSING_PROVENANCE": [bad[4]],
        "E6_NEGATION_RISK": [bad[5]],
    }
    fn_map = dict(CHECKS_ON_FACTS)
    for name, rows in single_checks.items():
        res = fn_map[name](rows)
        if not res:
            problems.append(f"{name}: 결함 데이터를 못 잡음 (미탐)")

    e3_res = check_e3_contradictory_duplicate(e3_pair)
    if not e3_res:
        problems.append("E3_CONTRADICTORY_DUPLICATE: 결함 데이터를 못 잡음 (미탐)")

    e7_bad = [{"condition_id": "c1", "fact_id": "f1", "condition_role": "???"}]
    e7_good = [{"condition_id": "c2", "fact_id": "f2", "condition_role": "selector"}]
    if not check_e7_condition_role_invalid(e7_bad):
        problems.append("E7_CONDITION_ROLE_INVALID: 결함 데이터를 못 잡음 (미탐)")
    if check_e7_condition_role_invalid(e7_good):
        problems.append("E7_CONDITION_ROLE_INVALID: 정상 데이터인데 오탐 발생")

    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    print("=== 1) 검사기 자기 시험 ===")
    problems = self_test()
    if problems:
        print("검사기 자체에 결함이 있어 본검사를 생략합니다:")
        for p in problems:
            print(" -", p)
        return
    print("자기 시험 통과 (결함 데이터는 걸리고, 정상 데이터는 조용함)\n")

    conn = sqlite3.connect(args.db)
    facts = _load_facts(conn)
    conditions = _load_conditions(conn)
    conn.close()

    print(f"=== 2) 본검사 — pension_facts {len(facts)}건, fact_conditions {len(conditions)}건 ===")
    result = {"db": args.db, "fact_count": len(facts), "condition_count": len(conditions),
              "checks": {}}
    total = 0
    for name, fn in CHECKS_ON_FACTS:
        hits = fn(facts)
        result["checks"][name] = hits
        total += len(hits)
        print(f"{name}: {len(hits)}건")
    e7_hits = check_e7_condition_role_invalid(conditions)
    result["checks"]["E7_CONDITION_ROLE_INVALID"] = e7_hits
    total += len(e7_hits)
    print(f"E7_CONDITION_ROLE_INVALID: {len(e7_hits)}건")

    result["total_findings"] = total
    out_path = BASE / "validate_facts_result.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {total}건 -> {out_path.name} 저장")
    if total:
        print("주의: 위 항목은 '확정 오류'가 아니라 '사람이 확인할 후보'입니다 —")
        print("특히 E2/E6은 오탐이 섞일 수 있으니 quote 원문을 직접 대조해 판단하세요.")


if __name__ == "__main__":
    main()
