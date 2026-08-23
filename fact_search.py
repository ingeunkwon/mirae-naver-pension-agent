#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
정형 사실(pension_facts) 키워드 검색 — RAG 검색이 놓치는 사실을 보완한다.

배경 (result_v1_fix2.json 11개 오답 재분석, 2026-08-23)
  Q-006/027/032/033/034 다섯 문항이 전부 "정답 문구가 retrieved_context에
  아예 없음"(context_ok=False)으로 떨어졌는데, diag_missing.py로 확인해보니
  다섯 건 모두 그 정답 문구가 이미 pension_facts 정형 DB에 정확한 값으로
  들어 있었다 (예: Q-032 "실물이전"·"2024년 10월 31일" → f_b2a741265541).
  즉 "합성 단계에서 빠뜨린 것"이 아니라 "RAG 검색(BM25+벡터+RRF)이 문서
  청크 760개 중 정답 청크를 top_k 안으로 못 끌어올린 것"이 근본 원인이었다.

  institution_rag_agent.py의 검색은 원문 문서 청크(rag3_*) 대상이라
  같은 사실이 표현을 바꿔가며 여러 청크에 흩어져 있으면 랭킹이 흔들린다.
  반면 pension_facts는 청크당 "사실 하나"로 이미 정제되어 있어 훨씬
  좁고 정확한 텍스트다 — 여기서 키워드로 직접 찾으면 RAG 랭킹에 기대지
  않고도 정답 문구를 근거로 올릴 수 있다.

  RAG를 대체하는 게 아니라 **보완**한다 — 기존 institution_rag 결과에
  추가로 붙여서 합성 프롬프트가 볼 근거를 넓힌다.
"""
from __future__ import annotations
import sqlite3
from bm25 import BM25

# BM25 랭킹의 최상위 대비 이 비율 미만인 사실은 버린다.
# institution_rag_agent.py는 0.35를 쓰지만(주 근거, 노이즈에 더 민감), 여기는
# **보완** 소스라 문턱을 더 낮춰(recall 우선) 둔다. pension_facts는
# validate_facts.py로 0건 결함 확인된 정제 데이터라 조금 더 끌어와도 틀린 값이
# 섞일 위험은 낮고, synthesize_answer의 "근거에 없는 조합은 답하지 않는다" 규칙이
# 관련 없는 항목은 생성 단계에서 걸러낸다. 반대로 문턱이 높으면(0.35) 결과가
# 정확히 있는데도(Q-034: 순위 9위, ratio 0.348 — 0.35에 근소하게 못 미침) 통째로
# 빠지는 게 더 위험하다.
KEEP_RATIO = 0.25

# 시도했다가 되돌린 것: 질의에 동의어("실물이전 이전 매도")를 덧붙이는 방식.
# Q-032엔 도움이 됐지만("동일제도 간 실물이전..." 사실이 8위 → 랭킹 안 흔들어도
# 이미 8위) Q-034("IRP를 연금저축으로 옮기려면...")에서 똑같이 "옮기"에 반응해
# 엉뚱한 "실물이전 가능 여부" 사실들 점수를 끌어올리는 바람에 정작 정답인
# "IRP↔연금저축계좌 이체 요건"(9위)을 11위로 더 밀어냈다 — 한 질문에 맞춘 동의어
# 확장이 다른 질문 랭킹을 흔드는 걸 직접 확인해서 뺐다. 대신 top_k/KEEP_RATIO를
# 넉넉히 잡아 augmentation 없이도 Q-032(8위)·Q-034(9위) 둘 다 통과하게 했다.


def _fact_text(r: dict) -> str:
    parts = [r.get("item"), r.get("condition_text"), r.get("value_text"),
             r.get("quote"), r.get("category"), r.get("pension_type")]
    return "\n".join(str(p) for p in parts if p and str(p) != "None")


class FactSearchIndex:
    """pension_facts 226행을 통째로 메모리에 올려 BM25로 찾는다.
    226행이라 인덱싱 비용이 무시할 만하다 — 매 요청 재구축해도 수 ms."""

    def __init__(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM pension_facts").fetchall()
        finally:
            conn.close()
        self.rows: list[dict] = [dict(r) for r in rows]
        self.bm25 = BM25([_fact_text(r) for r in self.rows]) if self.rows else None

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self.bm25 or not self.rows:
            return []
        raw = self.bm25.search(query, max(top_k * 4, 20))
        if not raw:
            return []
        top_score = raw[0][1]
        cut = top_score * KEEP_RATIO
        picked = [(i, s) for i, s in raw if s >= cut][:top_k]
        return [self.rows[i] for i, _ in picked]


def format_fact_evidence(facts: list[dict]) -> str:
    """synthesize_answer의 [제도 및 약관 문서 근거]에 이어 붙일 텍스트."""
    if not facts:
        return ""
    lines = ["[정형 사실 검색 결과 — pension_facts, RAG 랭킹과 무관하게 키워드로 직접 조회]"]
    for i, f in enumerate(facts, 1):
        item = f.get("item") or ""
        val = f.get("value_text") or ""
        cond = f.get("condition_text") or ""
        quote = f.get("quote") or ""
        src = f.get("source_file") or ""
        errata = f.get("errata_note")
        head = f"  {i}. {item}".rstrip()
        if val:
            head += f" = {val}"
        lines.append(head)
        if cond and cond != item:
            lines.append(f"     조건: {cond}")
        if quote:
            lines.append(f"     원문 인용: \"{quote}\"")
        if errata:
            lines.append(f"     참고: {errata}")
        if src:
            lines.append(f"     출처: {src}")
    return "\n".join(lines)
