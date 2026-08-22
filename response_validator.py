# -*- coding: utf-8 -*-
"""공모전 요구사항 격차분석 문서(claude/공모전_요구사항_격차분석)의 "검증 명세서"
절이 요구하는 L0(형식)/L2(팩트 대조) 검증을 순수 파이썬으로 구현한다.
API 호출이 필요 없어 응답을 막 내보내기 직전에 항상 돌릴 수 있다.

설계 원칙
---------
- **L0은 고친다.** 필수 필드가 비었거나 타입이 안 맞으면 안전한 값으로 채워
  넣는다. main.py가 이미 하던 개별적인 str()/빈 답변 보정을 한곳에 모은 것뿐.
- **L2는 막지 않고 드러낸다.** 답변에 등장한 숫자가 근거 텍스트 어디에도 없으면
  "확정 오류"가 아니라 "근거 미확인 후보"로만 think_trace에 남긴다. 반올림
  표현("약 150만원")이나 계산 결과처럼 정당하게 근거와 문자열이 다른 경우가
  섞여 있어서, 자동으로 답변을 바꾸거나 재시도시키면 지금 튜닝된 답변이
  오히려 깨질 위험이 더 크다 (RAG_MIN_TOP_SCORE를 캘리브레이션 전엔 꺼둔
  것과 같은 이유). 평가자가 볼 수 있게 think_trace에 남기는 것까지가 지금
  안전하게 할 수 있는 범위다.
"""
from __future__ import annotations
import re

REQUIRED_STR_FIELDS = ["question_id", "question", "retrieved_context",
                        "think_trace", "answer", "sources"]

FALLBACK_ANSWER = (
    "제공된 자료에서 관련 근거를 찾지 못했습니다. 질문을 조금 더 구체적으로 "
    "알려주시면 확인 가능한 범위에서 안내드리겠습니다.")


# --------------------------------------------------------------------- L0
def validate_l0_schema(payload: dict) -> tuple[dict, list[str]]:
    """필수 필드가 다 있고 전부 문자열인지 확인하고, 아니면 고쳐서 돌려준다.
    반환값: (고쳐진 payload, 발견한 문제 목록)"""
    issues = []
    fixed = dict(payload) if isinstance(payload, dict) else {}

    for field in REQUIRED_STR_FIELDS:
        val = fixed.get(field)
        if val is None:
            issues.append(f"L0: '{field}' 필드 없음 -> 빈 문자열로 채움")
            fixed[field] = ""
        elif not isinstance(val, str):
            issues.append(f"L0: '{field}' 타입이 {type(val).__name__} -> 문자열로 변환")
            fixed[field] = str(val)

    if not fixed.get("answer", "").strip():
        issues.append("L0: answer가 비어 있음 -> 기본 안내 문구로 대체")
        fixed["answer"] = FALLBACK_ANSWER

    return fixed, issues


# --------------------------------------------------------------------- L2
# 답변 안의 "숫자+단위" 조합을 찾는다. orchestrator.py의 프롬프트 규칙(★가장
# 중요)이 강제하는 항목(금액.비율.세율/기한.기간)과 같은 단위 집합을 쓴다.
_NUM = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_UNIT_PATTERNS = [
    re.compile(rf"({_NUM})\s*%"),
    re.compile(rf"({_NUM})\s*만\s*원"),
    re.compile(rf"({_NUM})\s*천\s*원"),
    re.compile(rf"({_NUM})\s*원"),
    re.compile(rf"({_NUM})\s*세"),
    re.compile(rf"({_NUM})\s*년\s*차"),
    re.compile(rf"({_NUM})\s*개?년"),
    re.compile(rf"({_NUM})\s*개월"),
    re.compile(rf"({_NUM})\s*일"),
    re.compile(rf"({_NUM})\s*등급"),
    re.compile(rf"({_NUM})\s*배"),
]


def _extract_number_unit_tokens(text: str) -> set[str]:
    if not text:
        return set()
    tokens = set()
    for pat in _UNIT_PATTERNS:
        for m in pat.finditer(text):
            tokens.add(m.group(0).replace(" ", ""))
    return tokens


def check_l2_fact_consistency(answer: str, retrieved_context: str) -> list[str]:
    """답변에 등장한 수치+단위 조합 중 근거 텍스트 어디에도 없는 것을 찾는다.
    확정 오류 목록이 아니라 사람이 볼 후보 목록이다."""
    if not answer or not retrieved_context:
        return []
    answer_tokens = _extract_number_unit_tokens(answer)
    if not answer_tokens:
        return []
    context_flat = retrieved_context.replace(" ", "")
    unverified = []
    for tok in sorted(answer_tokens):
        if tok not in context_flat:
            unverified.append(tok)
    return unverified


# --------------------------------------------------------------------- 통합
def validate_response(payload: dict) -> tuple[dict, list[str]]:
    """L0 -> L2 순서로 돌리고, payload(고쳐진 것)와 검증 로그 목록을 돌려준다.
    로그는 응답을 바꾸지 않고 think_trace 뒤에 붙이는 용도로만 쓴다."""
    fixed, l0_issues = validate_l0_schema(payload)
    log = list(l0_issues)

    unverified = check_l2_fact_consistency(fixed.get("answer", ""),
                                            fixed.get("retrieved_context", ""))
    if unverified:
        log.append("L2: 근거에서 문자 그대로 확인 안 되는 수치 후보 "
                    f"{len(unverified)}건 - {', '.join(unverified[:10])}")
    else:
        log.append("L2: 통과 (답변 속 수치가 근거 텍스트에서 확인됨)")

    if log:
        base = fixed.get("think_trace", "") or ""
        suffix = " | ".join(log)
        fixed["think_trace"] = f"{base} -> {suffix}" if base else suffix

    return fixed, log


# --------------------------------------------------------------------- self_test
def self_test() -> list[str]:
    problems = []

    # L0: 필드 누락/타입 오류를 고치는지
    bad_payload = {"question_id": 1, "question": "질문", "answer": None,
                    "retrieved_context": None}
    fixed, issues = validate_l0_schema(bad_payload)
    if not issues:
        problems.append("L0: 결함 payload인데 아무 문제도 못 찾음")
    for f in REQUIRED_STR_FIELDS:
        if not isinstance(fixed.get(f), str):
            problems.append(f"L0: 고친 뒤에도 '{f}'가 문자열이 아님")
    if fixed["answer"] != FALLBACK_ANSWER:
        problems.append("L0: 빈 answer를 기본 문구로 못 채움")

    # L0: 정상 payload는 그대로 통과
    good_payload = {f: "x" for f in REQUIRED_STR_FIELDS}
    good_payload["answer"] = "정상 답변입니다"
    _, good_issues = validate_l0_schema(good_payload)
    if good_issues:
        problems.append(f"L0: 정상 payload인데 오탐 -> {good_issues}")

    # L2: 근거에 없는 수치는 잡고, 있는 수치는 조용해야 한다
    ctx = "세액공제율은 16.5%이고 한도는 900만원이다."
    ans_ok = "세액공제율 16.5%를 적용하면 900만원까지 세액공제를 받을 수 있습니다."
    ans_bad = "세액공제율 22.5%를 적용하면 1200만원까지 받을 수 있습니다."
    if check_l2_fact_consistency(ans_ok, ctx):
        problems.append(f"L2: 정상 답변인데 오탐 -> {check_l2_fact_consistency(ans_ok, ctx)}")
    bad_hits = check_l2_fact_consistency(ans_bad, ctx)
    if "22.5%" not in bad_hits or "1200만원" not in bad_hits:
        problems.append(f"L2: 근거에 없는 수치를 못 잡음 -> {bad_hits}")

    return problems


if __name__ == "__main__":
    problems = self_test()
    if problems:
        print("자기 시험 실패:")
        for p in problems:
            print(" -", p)
    else:
        print("자기 시험 통과")
