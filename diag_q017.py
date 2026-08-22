# -*- coding: utf-8 -*-
"""Q-017 하나만 호출해서 answer 가 아니라 retrieved_context(진짜 SQL과
에러 메시지가 그대로 들어있음)를 통째로 파일에 저장한다.

모델이 답변 안에서 "SQL 의도는 이랬던 것 같다"고 요약해주는 건 모델의
추측이라 못 믿는다 — 실제로 무슨 SQL이 왜 실패했는지는 여기서 직접 봐야
한다.

사용법: python diag_q017.py  ->  diag_q017.json 에 저장
"""
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENDPOINT = "http://127.0.0.1:8000/answer"

QUESTIONS = {
    "Q-017": "연금저축으로 가입할 수 있는 채권형 펀드 중 총보수 0.3% 이하인 것 있나요?",
    "Q-014": "퇴직연금 계좌로 가입할 수 있는 펀드 중에 총보수가 가장 싼 게 뭔가요?",
}


def main():
    out = {}
    for qid, q in QUESTIONS.items():
        params = urllib.parse.urlencode({"question_id": qid, "question": q})
        url = f"{ENDPOINT}?{params}"
        with urllib.request.urlopen(url, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        out[qid] = body

    (BASE / "diag_q017.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved diag_q017.json")


if __name__ == "__main__":
    main()
