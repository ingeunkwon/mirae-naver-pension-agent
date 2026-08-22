# -*- coding: utf-8 -*-
"""v2 환각점검(coverage:none) 4문항만 따로 확인하는 스크립트.

eval_answers.py 전체를 콘솔로 다시 돌리면 파이프/코드페이지 인코딩 문제가
반복될 수 있어서, 이 스크립트는 화면에 아무것도 안 찍고 파일로만 저장한다
(파이썬이 파일을 직접 UTF-8로 쓰므로 콘솔 인코딩과 완전히 무관하다).

사용법:
    python check_hallu.py

    -> hallu_v2.json 에 결과 저장. 화면에는 "저장 완료"만 뜬다.
"""
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENDPOINT = "http://127.0.0.1:8000/answer"
TIMEOUT = 300

UNSURE = ["확인되지 않", "확인할 수 없", "찾지 못", "정보가 없", "자료에 없",
          "포함되어 있지 않", "알 수 없", "제공되지 않", "언급되어 있지 않",
          "나와 있지 않", "확인이 어렵"]


def norm(s):
    import re
    return re.sub(r"[\s,]", "", s or "")


def main():
    v2 = json.loads((BASE / "evalset_v2.json").read_text(encoding="utf-8"))
    questions = [q for q in v2["questions"] if q.get("coverage") == "none"]

    out = []
    for q in questions:
        params = urllib.parse.urlencode(
            {"question_id": q["question_id"], "question": q["question"]})
        url = f"{ENDPOINT}?{params}"
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            answer = body.get("answer", "")
            error = None
        except Exception as e:
            answer = ""
            error = f"{type(e).__name__}: {e}"

        said_unsure = any(norm(u) in norm(answer) for u in UNSURE)
        out.append({
            "question_id": q["question_id"],
            "question": q["question"],
            "said_unsure": said_unsure,
            "answer": answer,
            "error": error,
        })

    (BASE / "hallu_v2.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in out if r["said_unsure"])
    with open(BASE / "hallu_v2_summary.txt", "w", encoding="utf-8") as f:
        f.write(f"{ok}/{len(out)}\n")

    print(f"saved. {ok}/{len(out)}")


if __name__ == "__main__":
    main()
