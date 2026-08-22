# -*- coding: utf-8 -*-
"""RAG_MIN_TOP_SCORE 임계값 계산 — 결과를 파일로만 저장(콘솔 인코딩 문제 회피)."""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from rag_agent import PensionRAGAgent, hybrid_search  # noqa: E402

IN_CORPUS = [
    "만 72세에 연금을 받으면 세율이 몇 % 적용되나요?",
    "퇴직연금도 압류될 수 있나요?",
    "DB랑 DC 퇴직연금은 뭐가 다른가요?",
    "IRP에서 위험자산에 투자할 수 있는 한도가 얼마인가요?",
    "퇴직연금제도란 무엇인가요?",
    "연금소득이 연간 얼마를 넘으면 종합과세 대상이 되나요?",
    "명예퇴직금을 IRP에 넣으려면 언제까지 해야 하나요?",
]

OUT_OF_CORPUS = [
    "사전지정운용방법이 승인취소된 뒤 가입자가 별도로 운용지시하지 않으면 어떻게 되나요?",
    "과학기술인공제회 홈페이지 회원가입과 과학기술인연금 시스템 회원가입은 같은 건가요?",
    "문자나 이메일이 반송되면 퇴직연금 관련 통지는 어떻게 하나요?",
    "퇴직연금에서 어떤 경우를 상품의 '만기'로 보나요?",
    "비트코인을 퇴직연금 계좌에서 매수할 수 있나요?",
]


def top_score(agent, q):
    res = hybrid_search(q, agent.records, top_k=1)
    return res[0]["vector_score"] if res else 0.0


def main():
    agent = PensionRAGAgent()
    good = [{"q": q, "score": top_score(agent, q)} for q in IN_CORPUS]
    bad = [{"q": q, "score": top_score(agent, q)} for q in OUT_OF_CORPUS]

    lo_good = min(g["score"] for g in good)
    hi_bad = max(b["score"] for b in bad)
    separated = lo_good > hi_bad
    suggested = (hi_bad + (lo_good - hi_bad) * 0.4) if separated else min(g["score"] for g in good)

    result = {
        "good": good, "bad": bad,
        "lo_good": lo_good, "hi_bad": hi_bad,
        "separated": separated, "suggested_threshold": suggested,
    }
    (BASE / "calibrate_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved. separated={separated} suggested={suggested:.3f}")


if __name__ == "__main__":
    main()
