"""공모전 평가 API 서버.

    GET /answer?question_id={id}&question={질의}
    -> {question_id, question, retrieved_context, think_trace, answer, sources}
       (전부 문자열 — 공식 스펙은 5개 필드까지지만, "모든 답변에는 근거 문서
        표시할 것" 요건을 위해 sources를 추가 필드로 얹는다. 배열이 아니라
        문자열로 만들어서 "전부 문자열" 전제를 안 깬다.)

로컬 기동:
    python -m uvicorn main:app --host 127.0.0.1 --port 8000
"""
import sys
import time
import traceback

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from orchestrator import PensionOrchestrator
from response_validator import validate_response

app = FastAPI(title="Mirae Asset AI Festival 2026 - Pension Agent API")
orchestrator = PensionOrchestrator()

# 주최측 타임아웃은 300초. 여유를 두고 이 시간을 넘기면 있는 것만으로 응답한다.
DEADLINE = 240


def _format_sources(sources) -> str:
    """orchestrator.process()가 돌려주는 [{source_file, page, locator}, ...]를
    사람이 읽고 평가자가 확인하기 좋은 한 줄 문자열로 합친다. 근거 문서
    출처를 별도 필드로 명시하라는 요건(retrieved_context 안에도 "출처: ..."
    로 이미 들어 있지만, 긴 근거 텍스트에 묻히지 않게 따로 뽑아준다)."""
    if not sources:
        return ""
    seen: set[tuple] = set()
    labels = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        name = s.get("source_file")
        if not name:
            continue
        page, locator = s.get("page"), s.get("locator")
        loc_text = f"p.{page}" if page else (str(locator) if locator else "")
        key = (name, loc_text)
        if key in seen:
            continue
        seen.add(key)
        labels.append(f"{name} ({loc_text})" if loc_text else str(name))
    return "; ".join(labels)


@app.get("/answer")
def get_answer(
    question_id: str = Query(..., description="평가 질의 ID"),
    question: str = Query(..., description="평가 질의 원문")
):
    t0 = time.monotonic()
    result: dict = {}   # except 분기에서도 정의돼 있어야 밑의 core_facts 조회가 안전하다
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
            "sources": _format_sources(result.get("sources")),
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
            "sources": "",
        }

    # L0(형식 검사, 비어있으면 채움) -> L3(답변에 반영 안 된 핵심 근거 보강,
    # fact_search 상위 3건에 한해 좁게) -> L2(답변 속 수치가 근거에서 확인되는지
    # 대조, 로그만) 를 순수 파이썬으로 돌린다. L3만 answer를 바꿀 수 있고
    # (근거는 이미 찾았는데 LLM이 문장에 안 넣은 경우를 보강), 나머지는
    # think_trace 뒤에 로그만 남긴다. core_facts는 API 응답 payload에는
    # 없는 값이라 result에서 따로 꺼내 넘긴다.
    payload, _validation_log = validate_response(payload, result.get("core_facts"))

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
