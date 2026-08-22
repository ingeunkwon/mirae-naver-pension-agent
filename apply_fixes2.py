# -*- coding: utf-8 -*-
"""2차 패치 — 08/20 개선 이후 회귀 3건에 대한 코드 수정.

대상:
  Q-038 하드에러 : synthesize_answer() 가 HCX 응답 status_code 를 안 봄
                   (generate_answer() 는 이미 체크하는데 여기만 빠짐)
                 + SQL 조회 결과가 커지면(펀드 100종 텍스트 컬럼) 페이로드가
                   부풀어 오류 유발 가능 -> 방어적으로 길이 상한을 둔다.
  Q-039 무근거화 : "종류C-P" 류 클래스 코드 비교 질문이 FUND_WORDS 에
                   안 걸려 라우팅이 LLM 판단(비결정적)에 맡겨져 있었다.
                   클래스 코드 패턴을 규칙에 추가해 SQL_FUND 로 확정한다.
  환각점검 0/4  : "확인 필요 조건 -> 조건별 결론" 답변 구성 규칙과
                   "근거가 주제를 안 다루면 만들지 않는다" 규칙이 동시에 들어가
                   있어 충돌한다. 근거 자체가 없는 경우엔 후자가 이겨야 하는데
                   순서상 우선순위가 명시돼 있지 않았다. 우선순위 문구를 추가한다.

원본은 *.bak_20260820b 로 백업. python apply_fixes2.py --revert 로 되돌린다.
"""
from __future__ import annotations
import argparse
import ast
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SUFFIX = ".bak_20260820b"

PRECEDENCE_BLOCK = """
[우선순위 - 아래 두 규칙이 충돌하면 이 규칙이 이긴다]
근거가 질문의 주제를 아예 다루지 않으면(코퍼스에 없는 질문), "확인 필요 조건 ->
조건별 결론" 구성을 적용하지 않는다. 그 경우엔 "근거가 주제를 안 다루면 만들지
않는다" 규칙만 따라 "제공된 자료에서는 OO에 대한 내용을 확인하지 못했습니다."
로 짧게 답한다. 조건 하나가 빠진 것(예: 계좌 종류 미기재)과 근거 자체가 없는
것(질문 주제를 다루는 근거가 전혀 없음)을 구분해서 판단한다.
"""

RAG = []   # rag_agent.py 패치
ORC = []   # orchestrator.py 패치

# ── rag_agent.py ─────────────────────────────────────────────────────
RAG.append((
'''[평가 환경 - 중요]
평가는 단일턴 방식이다. 되묻고 다음 턴을 기다릴 수 없다.
따라서 확인이 필요한 조건이 있으면 그 확인 질문을 첫 답변 안에 포함하고,
동시에 조건별 결론까지 한 번에 제시한다.

[답변 구성 - 이 순서를 지킨다]''',
'''[평가 환경 - 중요]
평가는 단일턴 방식이다. 되묻고 다음 턴을 기다릴 수 없다.
따라서 확인이 필요한 조건이 있으면 그 확인 질문을 첫 답변 안에 포함하고,
동시에 조건별 결론까지 한 번에 제시한다.
''' + PRECEDENCE_BLOCK + '''
[답변 구성 - 이 순서를 지킨다]'''
))

# ── orchestrator.py ──────────────────────────────────────────────────
ORC.append((
'''[평가 환경 - 중요]
평가는 단일턴 방식이다. 되묻고 다음 턴을 기다릴 수 없다.
따라서 확인이 필요한 조건이 있으면 그 확인 질문을 첫 답변 안에 포함하고,
동시에 조건별 결론까지 한 번에 제시한다.

[답변 구성 - 이 순서를 지킨다]''',
'''[평가 환경 - 중요]
평가는 단일턴 방식이다. 되묻고 다음 턴을 기다릴 수 없다.
따라서 확인이 필요한 조건이 있으면 그 확인 질문을 첫 답변 안에 포함하고,
동시에 조건별 결론까지 한 번에 제시한다.
''' + PRECEDENCE_BLOCK + '''
[답변 구성 - 이 순서를 지킨다]'''
))

ORC.append((
'''import json
from pathlib import Path
import requests
from rag_agent import (PensionRAGAgent, build_headers, post_with_retry,
                       TIMEOUT, HCX_URL)
import pension_calc
from sql_agent import PensionSQLAgent

BASE_DIR = Path(__file__).resolve().parent''',
'''import json
import re
from pathlib import Path
import requests
from rag_agent import (PensionRAGAgent, build_headers, post_with_retry,
                       TIMEOUT, HCX_URL)
import pension_calc
from sql_agent import PensionSQLAgent

BASE_DIR = Path(__file__).resolve().parent

# 종류코드(C-P, C-P2, S-P2, C-RJ ...) 비교 질문은 '펀드' 같은 FUND_WORDS 가
# 안 걸려 라우팅이 LLM 판단(비결정적)에 맡겨졌다. Q-039 가 같은 질문인데도
# 실행마다 HYBRID/RAG 로 갈려서 결과가 달라졌다. 코드 패턴을 직접 잡는다.
CLASS_CODE_RE = re.compile(r"[CSA]-(?:P2?|R)[A-Z0-9]*", re.I)'''
))

ORC.append((
'''    def route_query(self, query: str) -> str:
        q = query.lower()
        has_fund = any(w in q for w in self.FUND_WORDS)
        has_fin = any(w in q for w in self.FIN_WORDS)
        has_cmp = any(w in q for w in self.COMPARE_WORDS)
        has_adv = any(w in q for w in self.ADVICE_WORDS)''',
'''    def route_query(self, query: str) -> str:
        q = query.lower()
        has_fund = any(w in q for w in self.FUND_WORDS)
        has_fin = any(w in q for w in self.FIN_WORDS)
        has_cmp = any(w in q for w in self.COMPARE_WORDS)
        has_adv = any(w in q for w in self.ADVICE_WORDS)
        if CLASS_CODE_RE.search(query):
            has_fund = True   # 클래스 코드가 보이면 펀드 신호로 취급'''
))

ORC.append((
'''        res = post_with_retry(HCX_URL, payload).json()
        return res.get("result", {}).get("message", {}).get("content", "").strip()''',
'''        response = post_with_retry(HCX_URL, payload)
        if response.status_code != 200:
            # generate_answer() 는 이미 이 체크가 있는데 여기만 빠져 있었다.
            # 체크 없이 .json() 을 호출하면 오류 응답 바디가 JSON 이 아닐 때
            # 원인을 알 수 없는 예외가 나고, main.py 가 통째로 삼켜서
            # "답변 생성 중 오류가 발생했습니다"만 남는다(Q-038).
            raise RuntimeError(f"HCX 오류 {response.status_code}: {response.text[:1000]}")
        res = response.json()
        return res.get("result", {}).get("message", {}).get("content", "").strip()'''
))

ORC.append((
'''            sql_context = "\\n".join(sql_parts)''',
'''            sql_context = "\\n".join(sql_parts)

            # 펀드 100종의 본문 텍스트 컬럼(fund_profiles/fund_sections)이
            # LIKE 검색에 걸리면 결과가 커져 HCX 요청이 크기 제한에 걸릴 수
            # 있다(Q-038 추정 원인). 방어적으로 앞부분만 쓴다.
            MAX_SQL_CONTEXT_CHARS = 6000
            if len(sql_context) > MAX_SQL_CONTEXT_CHARS:
                think_trace_list.append(
                    f"2-1. SQL 조회 결과가 커서 앞부분만 사용 (원본 {len(sql_context)}자)")
                sql_context = sql_context[:MAX_SQL_CONTEXT_CHARS] + "\\n...(생략)"'''
))

PATCHES = {"rag_agent.py": RAG, "orchestrator.py": ORC}


def apply_one(path: Path, patches, check_only: bool) -> str:
    original = path.read_text(encoding="utf-8")
    text = original
    for old, new in patches:
        n = text.count(old)
        if n != 1:
            raise ValueError(f"{path.name}: 대상 문자열을 정확히 1회 찾지 못함 (발견 {n}회)\n"
                              f"찾던 문자열 앞부분: {old[:80]!r}")
        text = text.replace(old, new, 1)
    ast.parse(text)
    if not check_only:
        backup = path.with_suffix(path.suffix + SUFFIX)
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        path.write_text(text, encoding="utf-8")
    return text


def revert():
    for name in PATCHES:
        path = BASE_DIR / name
        backup = path.with_suffix(path.suffix + SUFFIX)
        if backup.exists():
            path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"되돌림: {name}")
        else:
            print(f"백업 없음(건너뜀): {name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="적용하지 않고 검증만")
    ap.add_argument("--revert", action="store_true", help="백업으로 되돌리기")
    a = ap.parse_args()

    if a.revert:
        revert()
        return

    for name, patches in PATCHES.items():
        path = BASE_DIR / name
        try:
            apply_one(path, patches, check_only=a.check)
            print(f"{'검증 통과' if a.check else '적용 완료'}: {name} ({len(patches)}건)")
        except Exception as e:
            print(f"실패: {name}: {e}", file=sys.stderr)
            sys.exit(1)

    if not a.check:
        print("\n모든 패치 적용 완료. 되돌리려면: python apply_fixes2.py --revert")


if __name__ == "__main__":
    main()
