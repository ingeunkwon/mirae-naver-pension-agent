# -*- coding: utf-8 -*-
"""RAG_MIN_TOP_SCORE 임계값을 데이터로 정하기 위한 측정 스크립트.

왜 필요한가
-----------
hybrid_search 는 질문이 코퍼스 밖이어도 항상 top_k 개를 돌려준다.
관련성 임계값이 없으면 무관한 청크가 근거로 올라가고 생성 모델이 그 위에
이야기를 만든다 (평가 v2 환각 점검 1/4 — V-10 / V-21 / V-26).

임계값을 감으로 정하면 정상 문항까지 막을 수 있으므로,
"코퍼스에 있는 질문"과 "없는 질문"의 상위 유사도 분포를 실제로 재서 정한다.

사용법
------
    python calibrate_threshold.py

    -> 두 그룹의 top-1 코사인 유사도를 출력하고 임계값 후보를 제안한다.
       제안값을 .env 에 넣으면 켜진다.

           RAG_MIN_TOP_SCORE=0.42

       0 이거나 미설정이면 비활성(현재 기본값)이라 동작이 바뀌지 않는다.

질문당 임베딩 API 1회를 쓴다(총 12회). 1분이면 끝난다.
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from rag_agent import PensionRAGAgent, hybrid_search  # noqa: E402

# 코퍼스가 확실히 다루는 질문 (평가에서 통과한 문항)
IN_CORPUS = [
    "만 72세에 연금을 받으면 세율이 몇 % 적용되나요?",
    "퇴직연금도 압류될 수 있나요?",
    "DB랑 DC 퇴직연금은 뭐가 다른가요?",
    "IRP에서 위험자산에 투자할 수 있는 한도가 얼마인가요?",
    "퇴직연금제도란 무엇인가요?",
    "연금소득이 연간 얼마를 넘으면 종합과세 대상이 되나요?",
    "명예퇴직금을 IRP에 넣으려면 언제까지 해야 하나요?",
]

# 코퍼스에 근거가 없는 질문 (evalset_v2 의 coverage:none 문항)
OUT_OF_CORPUS = [
    "사전지정운용방법이 승인취소된 뒤 가입자가 별도로 운용지시하지 않으면 어떻게 되나요?",
    "과학기술인공제회 홈페이지 회원가입과 과학기술인연금 시스템 회원가입은 같은 건가요?",
    "문자나 이메일이 반송되면 퇴직연금 관련 통지는 어떻게 하나요?",
    "퇴직연금에서 어떤 경우를 상품의 '만기'로 보나요?",
    "비트코인을 퇴직연금 계좌에서 매수할 수 있나요?",
]


def top_score(agent: PensionRAGAgent, q: str) -> float:
    res = hybrid_search(q, agent.records, top_k=1)
    return res[0]["vector_score"] if res else 0.0


def main() -> None:
    print("임베딩 인덱스 로딩 중…")
    agent = PensionRAGAgent()
    print(f"  {len(agent.records):,}개 청크\n")

    print("=" * 66)
    print("코퍼스에 있는 질문 (막으면 안 되는 것)")
    print("=" * 66)
    good = []
    for q in IN_CORPUS:
        s = top_score(agent, q)
        good.append(s)
        print(f"  {s:.4f}  {q[:48]}")

    print("\n" + "=" * 66)
    print("코퍼스에 없는 질문 (막아야 하는 것)")
    print("=" * 66)
    bad = []
    for q in OUT_OF_CORPUS:
        s = top_score(agent, q)
        bad.append(s)
        print(f"  {s:.4f}  {q[:48]}")

    lo_good, hi_bad = min(good), max(bad)
    print("\n" + "=" * 66)
    print(f"코퍼스 내 최저 : {lo_good:.4f}")
    print(f"코퍼스 밖 최고 : {hi_bad:.4f}")
    print("=" * 66)

    if lo_good > hi_bad:
        # 두 분포가 겹치지 않는다 — 가운데를 잡되 정상 쪽에 여유를 둔다
        suggested = hi_bad + (lo_good - hi_bad) * 0.4
        print(f"\n두 분포가 분리됩니다. 임계값을 켜도 안전합니다.\n")
        print(f"  .env 에 추가:  RAG_MIN_TOP_SCORE={suggested:.3f}\n")
        print(f"  (코퍼스 밖 {hi_bad:.4f} 는 막고, 최저 정상 {lo_good:.4f} 는 통과)")
    else:
        print("\n⚠️  두 분포가 겹칩니다. 절대 임계값만으로는 깔끔히 못 가릅니다.")
        print(f"   겹치는 구간: {hi_bad:.4f} ~ {lo_good:.4f}")
        print("\n   선택지")
        print(f"   · 보수적으로 켜기 — RAG_MIN_TOP_SCORE={min(good):.3f} 미만만 차단")
        print("     (일부 환각은 남지만 정상 문항은 하나도 안 막힘)")
        print("   · 임계값 대신 프롬프트 강화에만 의존 (이미 적용됨)")
        print("   · 문항 수를 늘려 다시 측정")

    print("\n※ 값을 바꾼 뒤에는 반드시 evalset 을 다시 돌려 회귀를 확인하세요.")
    print("   python eval_answers.py --endpoint http://127.0.0.1:8000/answer \\")
    print("       --evalset evalset_v2.json --show")


if __name__ == "__main__":
    main()
