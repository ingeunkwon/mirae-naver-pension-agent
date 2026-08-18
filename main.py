from fastapi import FastAPI, Query
from orchestrator import PensionOrchestrator

app = FastAPI(title="Mirae Asset AI Festival 2026 - Pension Agent API")
orchestrator = PensionOrchestrator()

@app.get("/answer")
def get_answer(
    question_id: str = Query(..., description="평가 질의 ID"),
    question: str = Query(..., description="평가 질의 원문")
):
    """
    공모전 공식 평가 스키마 엔드포인트:
    Request: GET /answer?question_id={id}&question={text}
    Response: { question_id, question, retrieved_context, think_trace, answer }
    """
    result = orchestrator.process(question)
    
    return {
        "question_id": question_id,
        "question": question,
        "retrieved_context": result["retrieved_context"],
        "think_trace": result["think_trace"],
        "answer": result["answer"]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)