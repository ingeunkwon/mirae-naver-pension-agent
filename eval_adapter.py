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
"""
from orchestrator import PensionOrchestrator

_orch = PensionOrchestrator()


def answer(question: str) -> dict:
    """eval_answers.py가 요구하는 형식: answer_ok/context_ok 채점에 필요한
    answer, retrieved_context 를 dict로 돌려주면 된다. process()가 이미
    그 모양이라 그대로 넘긴다."""
    return _orch.process(question)
