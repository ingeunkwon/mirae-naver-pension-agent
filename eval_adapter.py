#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_answers.py의 --adapter 로 바로 넘길 수 있는 얇은 래퍼.

서버(uvicorn)를 안 띄우고도 orchestrator.process()를 곧바로 호출해서
같은 프로세스 안에서 40~66문항을 빠르게 돌린다 (--endpoint 로 하면
로컬 서버 기동 + HTTP 왕복까지 다 필요해서 이쪽이 더 간단하고 빠르다).

사용법 (이 파일이 있는 python/ 폴더에서):
    python eval_answers.py --adapter eval_adapter:answer --evalset evalset_v1.json --out result_v1_after_fix.json
    python eval_answers.py --adapter eval_adapter:answer --evalset evalset_v2.json --out result_v2_after_fix.json

주의: PensionOrchestrator()가 모듈 임포트 시 딱 한 번만 만들어진다
(매 질문마다 새로 만들면 institution_rag/fact_search 인덱싱을 40번 반복하게 된다).

주의 2: orchestrator.process()를 그냥 반환하면 실제 배포되는 main.py와 다른
결과가 나온다 — main.py는 항상 response_validator.validate_response()를
거쳐서 L0(형식)/L3(핵심 사실 누락 보강)/L2(팩트 대조 로그)까지 적용한 뒤
내보낸다. eval_answers.py는 "주최측이 실제로 받을 응답"을 채점하는 도구이므로
이 어댑터도 같은 경로를 그대로 태워야 한다 — 안 그러면 로컬 평가 점수가
실제 제출 결과보다 낮게(L3 보강이 빠진 채) 나온다.
"""
from orchestrator import PensionOrchestrator
from response_validator import validate_response

_orch = PensionOrchestrator()


def answer(question: str) -> dict:
    """eval_answers.py가 요구하는 형식: answer_ok/context_ok 채점에 필요한
    answer, retrieved_context 를 dict로 돌려준다. main.py의 /answer 핸들러와
    동일하게 validate_response를 거친 뒤 반환한다."""
    result = _orch.process(question)
    payload = {
        "question_id": "",
        "question": question,
        "retrieved_context": str(result.get("retrieved_context") or ""),
        "think_trace": str(result.get("think_trace") or ""),
        "answer": str(result.get("answer") or ""),
        "sources": "",
    }
    payload, _log = validate_response(payload, result.get("core_facts"))
    return payload
