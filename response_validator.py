# -*- coding: utf-8 -*-
"""공모전 요구사항 격차분석 문서(claude/공모전_요구사항_격차분석)의 "검증 명세서"
절이 요구하는 4레벨(L0 형식/L1 정책/L2 팩트/L3 목표 충족) 중 L0/L2/L3를
순수 파이썬으로 구현한다. API 호출이 필요 없어 응답을 막 내보내기 직전에
항상 돌릴 수 있다. (L1 정책은 orchestrator.safety_check가 이미 입력단에서
막고 있어 여기서는 다루지 않는다.)

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
- **L3(목표 충족)만 예외적으로 답변을 고친다 — 좁게.** result_v1_after_fix.json/
  result_v1_after_fix2.json을 직접 대조해서 확인한 사실: orchestrator의
  fact_search가 매번 정확한 근거("60일" 등)를 context에 넣어주는데도(둘 다
  context_ok=True), LLM이 그 숫자를 답변 문장에 넣을지 말지가 실행마다 달라져서
  같은 질문이 통과/탈락을 오간다(Q-029). 근거는 코드가 결정적으로 찾아줬는데
  그걸 답변에 반영하는지는 LLM 워딩 로또에 맡겨져 있던 것 — 이 좁은 범위(질의와
  가장 관련도 높은 상위 몇 개 정형 사실)에 한해서만 예외적으로 답변을 보강한다.
  전체 retrieved_context가 아니라 fact_search 상위 3건만 대상으로 삼는 이유도
  같다 — 대상을 넓히면 질문과 약하게만 관련된 사실까지 강제로 끼워 넣게 된다.
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


# --------------------------------------------------------------------- L3
def _fact_source_text(fact: dict) -> str:
    parts = [fact.get("item"), fact.get("condition_text"),
             fact.get("value_text"), fact.get("quote")]
    return " ".join(str(p) for p in parts if p and str(p) != "None")


def augment_missing_core_facts(answer: str, core_facts: list[dict] | None) -> tuple[str, list[str]]:
    """orchestrator가 이 질의와 가장 관련 높다고 판단한 정형 사실(core_facts,
    보통 상위 3건) 중, 그 사실의 숫자.단위(60일/1,800만원/5년 등)가 답변
    본문에 문자 그대로 없는 게 있으면 답변 끝에 짧게 보강한다.

    범위를 일부러 좁게 잡았다: 숫자.단위가 있는 사실만 본다(문장형 사실은
    "답변에 그 내용이 들어있는지"를 문자열만으로 판정할 수 없어 오탐 위험이
    크다), 그리고 orchestrator가 이미 상위 3건으로 추려준 것만 본다(그 이상은
    질문과의 관련도가 낮아질 수 있다). 근거 자체는 이미 fact_search가 항상
    안정적으로 찾아주고 있다는 게 확인됐으므로(context_ok는 매번 True), 여기서
    하는 일은 "찾아낸 근거를 답변이 실제로 언급했는지"만 코드로 강제하는 것 —
    새 근거를 만들어내지 않는다."""
    if not answer or not core_facts:
        return answer, []

    def norm(s: str) -> str:
        return re.sub(r"[\s,]", "", s or "")

    ans_norm = norm(answer)
    additions = []
    for fact in core_facts:
        src_text = _fact_source_text(fact)
        tokens = _extract_number_unit_tokens(src_text)
        if not tokens:
            continue   # 숫자.단위가 없는(문장형) 사실은 판정하지 않는다
        if any(norm(tok) in ans_norm for tok in tokens):
            continue   # 이미 답변에 그 사실의 핵심 숫자가 들어있다
        label = fact.get("item") or "확인된 사실"
        value = fact.get("value_text") or fact.get("condition_text") or ""
        line = f"- {label}: {value}".strip()
        if line not in additions:
            additions.append(line)

    if not additions:
        return answer, []

    augmented = (answer.rstrip() + "\n\n[추가 확인 사항 — 검색된 근거 중 위 "
                 "답변에 반영되지 않은 항목]\n" + "\n".join(additions))
    log = [f"L3: 근거 검색은 됐으나 답변에 반영 안 된 핵심 사실 {len(additions)}건 "
           f"보강 - {'; '.join(additions)}"]
    return augmented, log


# --------------------------------------------------------------------- 통합
def validate_response(payload: dict, core_facts: list[dict] | None = None) -> tuple[dict, list[str]]:
    """L0 -> L3 -> L2 순서로 돌리고, payload(고쳐진 것)와 검증 로그 목록을 돌려준다.
    L3만 answer를 바꿀 수 있고(누락된 핵심 사실 보강), 나머지는 think_trace에만
    남긴다. core_facts는 API 응답 payload에는 없는, orchestrator.process()가
    검증 전용으로 넘기는 내부 값이다."""
    fixed, l0_issues = validate_l0_schema(payload)
    log = list(l0_issues)

    fixed["answer"], l3_log = augment_missing_core_facts(fixed.get("answer", ""), core_facts)
    log.extend(l3_log)

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

    # L3: Q-029 실제 재현 사례 — "60일"이 빠진 답변에 보강되는지
    core_facts = [{"item": "명예퇴직금·퇴직위로금 IRP 입금 기한",
                   "value_text": "지급받은 시점부터 60일 이내",
                   "quote": "퇴직 시 명예퇴직금, 퇴직위로금을 받은 경우 지급받은 "
                            "시점부터 60일 이내에 IRP로 일부 또는 전부 입금도 가능합니다."}]
    ans_missing = "명퇴수당을 연금계좌에 입금하면 절세 효과가 있습니다."
    augmented, l3_log = augment_missing_core_facts(ans_missing, core_facts)
    if "60일" not in augmented:
        problems.append(f"L3: 누락된 핵심 사실을 못 보강함 -> {augmented!r}")
    if not l3_log or not l3_log[0].startswith("L3:"):
        problems.append(f"L3: 보강했는데 로그가 안 남음 -> {l3_log}")

    # L3: 이미 답변에 있으면 중복으로 덧붙이면 안 된다
    ans_has_it = "60일 이내에 IRP로 입금하면 절세 효과가 있습니다."
    augmented2, l3_log2 = augment_missing_core_facts(ans_has_it, core_facts)
    if augmented2 != ans_has_it or l3_log2:
        problems.append(f"L3: 이미 있는 사실인데 중복 보강함 -> {augmented2!r}")

    # L3: 숫자.단위가 없는 문장형 사실은 판정하지 않는다(오탐 방지)
    core_facts_sentence = [{"item": "실물이전 가능 여부", "value_text": "실물이전 불가, 현금이전"}]
    augmented3, l3_log3 = augment_missing_core_facts("전액 현금으로 이전해야 합니다.",
                                                       core_facts_sentence)
    if augmented3 != "전액 현금으로 이전해야 합니다." or l3_log3:
        problems.append(f"L3: 문장형 사실인데 잘못 보강함 -> {augmented3!r}")

    # L3: core_facts가 없으면 아무 것도 안 건드린다
    augmented4, l3_log4 = augment_missing_core_facts(ans_missing, None)
    if augmented4 != ans_missing or l3_log4:
        problems.append("L3: core_facts가 없는데도 답변을 건드림")

    # 통합: validate_response가 L0 -> L3 -> L2 순서로 다 돌고 think_trace에 남기는지
    payload = {f: "x" for f in REQUIRED_STR_FIELDS}
    payload["answer"] = ans_missing
    payload["retrieved_context"] = core_facts[0]["quote"]
    payload["think_trace"] = "기존 로그"
    fixed, log = validate_response(payload, core_facts)
    if "60일" not in fixed["answer"]:
        problems.append("통합: validate_response가 L3 보강을 반영 안 함")
    if "60일" not in fixed["think_trace"]:
        problems.append("통합: L3 보강 로그가 think_trace에 안 남음")
    if not any(l.startswith("L2:") for l in log):
        problems.append("통합: L2 로그가 빠짐 (L3가 L2를 덮어쓴 것으로 보임)")

    return problems


if __name__ == "__main__":
    problems = self_test()
    if problems:
        print("자기 시험 실패:")
        for p in problems:
            print(" -", p)
    else:
        print("자기 시험 통과")
