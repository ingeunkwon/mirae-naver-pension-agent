# -*- coding: utf-8 -*-
"""연금수령한도 계산 — LLM 에게 산수를 맡기지 않는다.

    연금수령한도 = 연금계좌 평가액 / (11 - 연금수령연차) x 120%

  - 1년차  : 분모 10 (한도가 가장 좁다)
  - 10년차 : 분모 1  (평가액의 120% 까지)
  - 11년차~: 한도 없음 (전액 인출해도 연금수령으로 인정)

왜 코드로 계산하는가
--------------------
HCX 계열 모델이 이 공식에서 x120% 를 x(11-연차) 로 잘못 적용해 8배 틀린 값을
내는 사례가 반복 보고됐다. 프롬프트로는 안 고쳐지는 산수 오류라 파이썬으로
직접 계산해 "이 값을 그대로 쓰라"고 근거에 넣어준다.

평가 Q-030("올해 55세인데 연금계좌 평가액이 1억이면 올해 얼마까지 받을 수
있나요?")은 검색이 빈손이라 답변 자체를 포기했던 문항이다.

트리거는 좁게 잡는다. 미탐(계산이 안 켜짐)은 그냥 검색 답변으로 대체되지만,
오탐(엉뚱한 계산을 사실처럼 제시)은 훨씬 위험하다.
"""
from __future__ import annotations

import re

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(조|억|천만|백만|만|원)")
_UNITS = {"조": 10**12, "억": 10**8, "천만": 10**7,
          "백만": 10**6, "만": 10**4, "원": 1}

_YEAR_RE = re.compile(r"(\d+)\s*년\s*차")
_YEAR_RE2 = re.compile(r"(\d+)\s*년째")
_AGE_RE = re.compile(r"(?:만\s*)?(\d{2})\s*세")

_BALANCE_WORDS = ["평가액", "적립금", "적립액", "잔액", "평가금액"]
_TOPIC_WORDS = ["연금수령한도", "수령한도", "얼마까지", "얼마나 받을 수",
                "수령할 수 있", "받을 수 있나", "받을 수 있는지", "인출할 수 있"]


def parse_won(text: str) -> int | None:
    """'1억', '1억 2,000만원', '100,000,000원' 을 원 단위 정수로."""
    total, found = 0.0, False
    for m in _AMOUNT_RE.finditer(text.replace(",", "")):
        total += float(m.group(1)) * _UNITS[m.group(2)]
        found = True
    return int(round(total)) if found else None


def extract_year(text: str) -> tuple[int | None, str]:
    """연금수령연차를 뽑는다. 없으면 나이에서 '올해 최초 개시'로 추정한다."""
    m = _YEAR_RE.search(text) or _YEAR_RE2.search(text)
    if m:
        return int(m.group(1)), "질문에 명시된 연차"
    m = _AGE_RE.search(text)
    if m:
        age = int(m.group(1))
        if age < 55:
            return 0, f"만 {age}세는 연금수령 개시연령(55세) 미만"
        return age - 54, f"만 {age}세를 올해 최초 연금개시로 가정 -> {age - 54}년차"
    return None, "연차·나이 정보를 찾지 못함"


def format_won(amount: float) -> str:
    amount = int(round(amount))
    eok, rem = divmod(amount, 10**8)
    man, won = divmod(rem, 10**4)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man:,}만원")
    if won or not parts:
        parts.append(f"{won:,}원")
    return " ".join(parts)


def needs_calc(question: str) -> bool:
    """평가액 언급 + 금액 표현 + 나이/연차가 전부 있어야 켠다."""
    if not any(w in question for w in _BALANCE_WORDS):
        return False
    if parse_won(question) is None:
        return False
    year, _ = extract_year(question)
    if year is None:
        return False
    return any(w in question for w in _TOPIC_WORDS)


def compute(question: str) -> str | None:
    """계산 결과를 근거 텍스트로 돌려준다. 조건이 안 맞으면 None."""
    if not needs_calc(question):
        return None
    amount = parse_won(question)
    year, basis = extract_year(question)

    if year < 1:
        return (f"[연금수령한도 계산] {basis}이므로 아직 연금수령 요건"
                f"(만 55세 이상)을 충족하지 못한 것으로 보입니다. "
                f"연금수령한도 계산 대상이 아닙니다.")
    if year >= 11:
        return (f"[연금수령한도 계산] 연금수령연차 {year}년차({basis})는 "
                f"11년차 이상이므로 연금수령한도가 없습니다. "
                f"평가액 전액을 인출해도 연금수령으로 인정됩니다.")

    limit = amount / (11 - year) * 1.2
    return (
        f"[연금수령한도 계산]\n"
        f"  공식: 연금수령한도 = 연금계좌 평가액 / (11 - 연금수령연차) x 120%\n"
        f"  대입: {format_won(amount)} / (11 - {year}) x 120% "
        f"= {format_won(limit)}\n"
        f"  연차 산정 근거: {basis}\n"
        f"  ※ 이 값은 코드로 직접 계산한 것입니다. 답변에서 이 숫자를 그대로 쓰고\n"
        f"     다시 계산하지 마십시오. 한도를 넘겨 인출하면 초과분은 연금외수령으로\n"
        f"     분류되니 그 점도 함께 안내하십시오."
    )


if __name__ == "__main__":
    tests = [
        "올해 55세인데 연금계좌 평가액이 1억이면 올해 얼마까지 연금으로 받을 수 있나요?",
        "연금계좌 평가액 2억, 연금수령 3년차인데 연금수령한도가 얼마인가요?",
        "평가액 5천만원이고 12년차입니다. 얼마까지 받을 수 있나요?",
        "만 50세인데 평가액 1억이면 얼마까지 받을 수 있나요?",
        "퇴직연금 중도인출 사유가 뭐야?",
        "연금수령한도를 넘겨서 인출하면 어떻게 되나요?",
    ]
    for t in tests:
        print(f"\nQ: {t}")
        r = compute(t)
        print("   ->", (r if r else "(계산 대상 아님 — 검색 답변으로 처리)"))
