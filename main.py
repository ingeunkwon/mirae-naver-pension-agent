"""공모전 평가 API 서버.

    GET /answer?question_id={id}&question={질의}
    -> {question_id, question, retrieved_context, think_trace, answer}  (전부 문자열)

로컬 기동:
    python -m uvicorn main:app --host 127.0.0.1 --port 8000
"""
import sys
import time
import traceback

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from orchestrator import PensionOrchestrator

app = FastAPI(title="Mirae Asset AI Festival 2026 - Pension Agent API")
orchestrator = PensionOrchestrator()

# 주최측 타임아웃은 300초. 여유를 두고 이 시간을 넘기면 있는 것만으로 응답한다.
DEADLINE = 240


@app.get("/answer")
def get_answer(
    question_id: str = Query(..., description="평가 질의 ID"),
    question: str = Query(..., description="평가 질의 원문")
):
    t0 = time.monotonic()
    try:
        result = orchestrator.process(question)
        answer = str(result.get("answer") or "")
        if not answer.strip():
            answer = ("제공된 자료에서 관련 근거를 찾지 못했습니다. "
                      "질문을 조금 더 구체적으로 알려주시면 확인 가능한 범위에서 "
                      "안내드리겠습니다.")
        payload = {
            "question_id": str(question_id),
            "question": str(question),
            "retrieved_context": str(result.get("retrieved_context") or ""),
            "think_trace": str(result.get("think_trace") or ""),
            "answer": answer,
        }
    except Exception as exc:
        # 예외가 그대로 새면 500 이 나가고 그 문항은 통째로 0점이 된다.
        # 규격에 맞는 200 응답이 재시도 낭비도 적고 안전하다.
        print(f"[server] /answer 예외: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        payload = {
            "question_id": str(question_id),
            "question": str(question),
            "retrieved_context": "",
            "think_trace": f"처리 중 오류 발생: {type(exc).__name__}",
            "answer": ("죄송합니다. 답변 생성 중 오류가 발생했습니다. "
                       "잠시 후 다시 시도해 주세요."),
        }

    elapsed = time.monotonic() - t0
    print(f"[server] {question_id} {elapsed:.1f}초", file=sys.stderr)
    if elapsed > DEADLINE:
        print(f"[server] 경고: {elapsed:.0f}초 — 300초 한도에 근접", file=sys.stderr)
    return JSONResponse(content=payload, media_type="application/json")


@app.get("/health")
def health():
    """생존 확인용. 평가 규격에는 없는 엔드포인트."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
