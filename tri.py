#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3치 논리 (Kleene) — 부분 정보를 타입으로 표현한다.

왜 필요한가
  리뷰에서 지적된 결함 6건이 전부 같은 뿌리였다. **unknown이 타입에 없어서
  지나가는 지점마다 조용히 True 또는 False로 붕괴한다.**

    · 복합 requirement   OR 한 갈래만 참이면 전체를 met으로 판정  → true로 붕괴
    · calc 생성          요건 미확인인데 계산값을 확정 수치로      → true로 붕괴
    · validator OR       condition_group>0을 검사에서 제외        → 양쪽으로 붕괴
    · E3 boolean gap     0과 1 사이에 값이 없다 = 구멍            → false로 붕괴
    · 6년차 fact         '=6'이라 7년차를 미충족으로 판정          → false로 붕괴

  그래서 개별 패치가 아니라 타입을 하나 만들고 그 지점들을 통과시킨다.

Kleene 3치 진리표 (여기서 쓰는 정의)

    AND   T  F  U        OR    T  F  U        NOT
     T    T  F  U         T    T  T  T         T -> F
     F    F  F  F         F    T  F  U         F -> T
     U    U  F  U         U    T  U  U         U -> U

  핵심은 두 줄이다.
    F AND U = F     확정 미충족은 unknown이 있어도 미충족이다 (되물을 필요 없음)
    F OR  U = U     한 갈래가 거짓이어도 다른 갈래를 모르면 전체를 모른다
                    ← 지적 1번이 정확히 이 칸이다

용어
  판정값은 문자열 상수 MET / UNMET / UNKNOWN 을 쓴다. 그대로 evidence의
  requirement_status 필드가 되고 think_trace에 찍히므로, enum보다 문자열이
  디버깅에 유리하다.
"""
from __future__ import annotations

MET = "met"
UNMET = "unmet"
UNKNOWN = "unknown"

VALUES = (MET, UNMET, UNKNOWN)


def from_bool(v) -> str:
    """True/False/None → met/unmet/unknown."""
    if v is None:
        return UNKNOWN
    return MET if v else UNMET


def and_(values) -> str:
    """Kleene AND. 빈 입력은 MET(공허참)."""
    seen_unknown = False
    for v in values:
        if v == UNMET:
            return UNMET          # F AND U = F — 확정 미충족이 이긴다
        if v == UNKNOWN:
            seen_unknown = True
    return UNKNOWN if seen_unknown else MET


def or_(values) -> str:
    """Kleene OR. 빈 입력은 UNMET(공허거짓)."""
    seen_unknown = False
    any_value = False
    for v in values:
        any_value = True
        if v == MET:
            return MET            # T OR U = T
        if v == UNKNOWN:
            seen_unknown = True
    if not any_value:
        return UNMET
    return UNKNOWN if seen_unknown else UNMET


def combine_components(components, observed: bool = False) -> str | None:
    """fact 하나의 최종 requirement_status.

    components: [(verdict, [missing_key, ...]), ...]
    observed  : 질문이 이 fact의 조건 중 **하나라도 실제로 관측했는가**

    None(미판정)과 UNKNOWN을 가르는 기준은 `observed`다.
      · None    질문이 이 fact를 건드리지도 않았다 → 판정 자체를 안 한다
      · UNKNOWN 이 fact는 질문과 관련이 있는데 요건을 확정할 수 없다 → **되물어야 한다**

    ★ partial-observation 결함 (v4.0에서 틀렸던 곳)
      v4.0은 '요건 성분이 전부 UNKNOWN이면 무조건 None'이었다. 그래서
          '요양으로 IRP 중도인출 되나요?'   (요양 **기간**은 미지정)
      에서 withdrawal_reason은 관측됐고 그 fact가 선택까지 됐는데, 유일한 요건인
      care_months>=6이 미관측이라 status=None이 됐다.
      → 답변은 아무 유보 없이 '중도인출 가능'으로 읽힌다. 6개월 요건이 사라진다.

      요건이 **하나뿐인** fact가 통째로 검증에서 빠지는 구조라, 조건이 많은
      fact보다 조건이 적은 fact가 더 위험해지는 역전이 있었다.

      관측이 있었으면 UNKNOWN이다. 관측이 아예 없을 때만 None이다.
      이러면 '조건 붙은 fact가 전부 unknown이 되는' 남발도 함께 막힌다 —
      질문이 건드리지 않은 fact는 여전히 None이기 때문이다.
    """
    verdicts = [v for v, _ in components]
    if not verdicts:
        return None
    if all(v == UNKNOWN for v in verdicts):
        return UNKNOWN if observed else None
    return and_(verdicts)


def missing_keys(components) -> list[str]:
    """UNKNOWN인 component가 필요로 했던 조건 키. 되묻기 문구의 재료다."""
    out: list[str] = []
    for verdict, keys in components:
        if verdict != UNKNOWN:
            continue
        for k in keys:
            if k not in out:
                out.append(k)
    return out
