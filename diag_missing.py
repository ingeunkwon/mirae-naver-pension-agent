#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Q-006/027/032/033/034/038 이 왜 context_ok=False(근거 자체에 정답 문구가 없음)인지 진단.

각 질문에 대해:
  1. 임베딩 코퍼스 전체 텍스트에서 정답 문구가 "어딘가에는" 존재하는지 (grep) 확인
     -> 없으면 코퍼스 자체에 그 사실이 없는 것(데이터 갭, 코드로 못 고침)
     -> 있으면 어느 청크(chunk_id/source_file)에 있는지 특정
  2. 그 청크가 실제 검색 파이프라인(BM25+벡터+RRF+리랭커) top_k 안에 들어오는지 확인
     -> 안 들어오면 검색 랭킹 문제(코드로 고칠 여지 있음)
  3. pension_facts SQLite 안에도 그 수치가 있는지 확인 (정형 데이터 경로)
"""
import json, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EMB_PATH = "data/vector_db/junil_rag_embeddings_v3.json"
chunks = json.load(open(EMB_PATH, encoding="utf-8"))

CASES = [
    ("Q-006", "IRP에 넣은 돈은 세액공제를 안 받았어도 나중에 꺼낼 때 세금을 내나요?",
     ["과세제외", "비과세"]),
    ("Q-027", "IRP에 돈을 넣는 방법에는 뭐가 있나요?",
     ["1,800만원", "1800만원", "본인이 납입", "개인부담금", "본인 부담금"]),
    ("Q-032", "퇴직연금 계좌를 다른 증권사로 옮길 때 갖고 있던 펀드를 팔지 않아도 되나요?",
     ["실물이전", "2024년 10월 31일", "2024.10.31", "2024-10-31"]),
    ("Q-033", "실물이전으로 옮길 수 없는 상품에는 뭐가 있나요?",
     ["디폴트옵션", "리츠", "REITs", "REIT"]),
    ("Q-034", "IRP를 연금저축으로 옮기려면 조건이 있나요?",
     ["5년 경과", "5년이 경과", "가입 후 5년"]),
    ("Q-038", "펀드를 오후 3시 30분 전에 환매 신청하면 언제 돈을 받나요?",
     ["제2영업일", "제4영업일"]),
]

def full_text(c):
    parts = [c.get("title"), c.get("major_title"), c.get("sub_title"),
             c.get("table_title"), c.get("question"), c.get("text")]
    return "\n".join(str(p) for p in parts if p and str(p) != "None")

print("="*100)
print("1) 코퍼스 전체(청크 %d개)에서 정답 문구 존재 여부" % len(chunks))
print("="*100)
hits_by_case = {}
for qid, q, phrases in CASES:
    print(f"\n--- {qid}: {q}")
    found_any = False
    hit_chunk_ids = []
    for ph in phrases:
        matches = [c for c in chunks if ph in full_text(c)]
        if matches:
            found_any = True
            for m in matches[:5]:
                cid = m.get("chunk_id") or m.get("source_record_id") or m.get("title")
                hit_chunk_ids.append(cid)
                print(f"    '{ph}' 존재 -> chunk_id={cid} source_file={m.get('source_file')} title={m.get('title')}")
    hits_by_case[qid] = hit_chunk_ids
    if not found_any:
        print("    (코퍼스 전체 어디에도 없음 — 데이터 자체가 없는 진짜 갭)")

# pension_facts / fact_conditions 도 확인
print("\n" + "="*100)
print("2) pension_facts / fact_conditions 정형 DB에서도 확인")
print("="*100)
conn = sqlite3.connect("data/pension_rules.db")
cur = conn.cursor()
for qid, q, phrases in CASES:
    for ph in phrases:
        like = f"%{ph}%"
        try:
            rows = cur.execute(
                "SELECT fact_id, item, value_text, quote FROM pension_facts "
                "WHERE condition_text LIKE ? OR value_text LIKE ? OR quote LIKE ? LIMIT 3",
                (like, like, like)).fetchall()
        except sqlite3.OperationalError as e:
            rows = []
        if rows:
            print(f"{qid}: '{ph}' -> pension_facts {len(rows)}건")
            for r in rows:
                print("   ", r)
conn.close()

print("\n" + "="*100)
print("3) 실제 검색 파이프라인이 그 청크를 top_k 안에 올리는지 (API키 없어도 BM25 경로로 확인 가능)")
print("="*100)
from institution_rag_agent import PensionRAGAgent
agent = PensionRAGAgent(embedding_file=EMB_PATH)
for qid, q, phrases in CASES:
    hit_ids = set(str(x) for x in hits_by_case.get(qid, []) if x)
    if not hit_ids:
        print(f"\n{qid}: 코퍼스에 정답 문구 자체가 없어 검색 랭킹 문제인지 판단 불가 (스킵)")
        continue
    out = agent.run(q, top_k=6)
    ev = out["evidence"]
    ret_ids = set()
    for e in ev:
        p = e.get("provenance") or {}
        ret_ids.add(str(p.get("chunk_id") or ""))
    overlap = hit_ids & ret_ids
    print(f"\n{qid}: 정답 청크 후보={hit_ids}")
    print(f"   top_k=6 검색 결과 청크={ret_ids}")
    print(f"   -> {'검색됨 (합성 단계 문제)' if overlap else '검색 안 됨 (검색 랭킹 문제)'}")
    for t in out["think_trace"]:
        print("      ·", t)
