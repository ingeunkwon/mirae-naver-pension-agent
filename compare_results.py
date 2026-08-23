#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_answers.py --out 로 저장한 채점 결과 두 개(수정 전/후)를 비교해서
"오늘 고친 문항 말고 원래 맞던 문항이 흔들렸는지"를 한눈에 보여준다.

사용법:
    python compare_results.py result_v1_fix2.json result_v1_after_fix.json
    python compare_results.py result_v2_fix1.json result_v2_after_fix.json

읽는 법:
    새로 통과 -> 이번 수정이 실제로 고친 문항
    새로 탈락 -> ★ 회귀(regression). topics/aspects 보너스가 institution_rag
                랭킹을 흔들면서 원래 맞던 문항의 근거 순위가 바뀌었을 가능성.
                반드시 answer 전문을 열어 원인을 확인할 것.
"""
import json
import sys


def load(path):
    data = json.loads(open(path, encoding="utf-8").read())
    rows = {r["question_id"]: r for r in data.get("rows", [])}
    return data.get("answer_rate"), rows


def main():
    if len(sys.argv) != 3:
        sys.exit("사용법: python compare_results.py <이전_result.json> <이후_result.json>")
    before_path, after_path = sys.argv[1], sys.argv[2]
    before_rate, before = load(before_path)
    after_rate, after = load(after_path)

    all_ids = sorted(set(before) | set(after))
    newly_pass, newly_fail, still_fail, still_pass, only_one_side = [], [], [], [], []

    for qid in all_ids:
        b = before.get(qid)
        a = after.get(qid)
        if b is None or a is None:
            only_one_side.append((qid, b is not None, a is not None))
            continue
        bo, ao = bool(b.get("answer_ok")), bool(a.get("answer_ok"))
        if not bo and ao:
            newly_pass.append(qid)
        elif bo and not ao:
            newly_fail.append(qid)
        elif ao:
            still_pass.append(qid)
        else:
            still_fail.append(qid)

    print(f"이전: {before_path}  (answer_rate={before_rate})")
    print(f"이후: {after_path}  (answer_rate={after_rate})")
    print(f"문항 수: {len(all_ids)}\n")

    print(f"[새로 통과] {len(newly_pass)}건 — 이번 수정으로 고쳐진 문항")
    for qid in newly_pass:
        print(f"  + {qid}  {after[qid].get('question', '')[:50]}")

    print(f"\n[새로 탈락] {len(newly_fail)}건 — ★ 회귀 의심, 반드시 확인 필요")
    for qid in newly_fail:
        b, a = before[qid], after[qid]
        print(f"  - {qid}  {a.get('question', '')[:50]}")
        print(f"      이전 missing: {b.get('missing_in_answer')}")
        print(f"      이후 missing: {a.get('missing_in_answer')}")

    print(f"\n[변화 없음] 계속 통과 {len(still_pass)}건 / 계속 탈락 {len(still_fail)}건")
    if only_one_side:
        print(f"\n[주의] 한쪽에만 있는 문항 {len(only_one_side)}건 (question_id 불일치/누락 가능성)")
        for qid, in_b, in_a in only_one_side:
            print(f"  ? {qid}  이전={'있음' if in_b else '없음'} 이후={'있음' if in_a else '없음'}")

    print("\n" + "=" * 60)
    if newly_fail:
        print(f"★★★ 회귀 {len(newly_fail)}건 발생 — 제출 전 반드시 answer 전문을 열어 원인 확인 ★★★")
    else:
        print("회귀 없음 — 새로 탈락한 문항이 없습니다.")


if __name__ == "__main__":
    main()
