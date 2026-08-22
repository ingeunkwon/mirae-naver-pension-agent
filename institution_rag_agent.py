#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
제도 RAG Agent — 하이브리드 검색 (벡터 + BM25) → RRF → 메타데이터 보너스 → 리랭커

v2 → v3 수정
  1. **메타데이터 보너스 배율 버그**
     v2는 `base * (1 + bonus * 10)` 이었다. PENSION_TYPE_BONUS=0.08은
     이름상 +8%인데 실제로는 1 + 0.08×10 = 1.8, 즉 +80%였다.
     제도·주제·aspect가 다 맞으면 최대 2.9배까지 뛰어서, 검색 40위가
     메타데이터 하나로 1위를 넘을 수 있었다. → `base * (1 + bonus)`.
     최종값은 평가셋으로 정한다. 환경변수로 바꿀 수 있게 열어뒀다.
  2. **실패한 질의 임베딩을 캐시하던 문제**
     API가 한 번 실패해 None이 캐시되면 같은 질문은 이후 계속 BM25 단독으로
     갔다. 성공한 벡터만 캐시한다.
  3. **차원 검증** — 적재 시 임베딩 차원이 일정한지, 질의 벡터와 맞는지 본다.
     차원이 어긋나면 코사인이 조용히 이상한 값을 낸다.
  4. **time_scope 필드명 통일** — 임베딩 파일에는 `time_scope`와
     `source_time_scope`가 둘 다 있다. 둘 다 읽어 하나로 쓴다.
     한쪽만 보면 historical 감점이 조용히 죽는다.
  5. **관련도 없는 후보를 억지로 채우지 않는다**
     BM25 상위 점수 대비 일정 비율 미만은 후보에서 뺀다. top_k만큼 무조건
     반환하지 않는다.

필터링 원칙은 그대로다 — 애매하면 필터를 걸지 않는다. 메타데이터는
**가산점**으로만 쓰고 배제에는 쓰지 않는다.

API 없이도 동작한다. CLOVASTUDIO_API_KEY가 없으면 벡터·리랭커를 건너뛰고
BM25만 쓴다. 어느 경로로 돌았는지 think_trace에 남는다.
"""
from __future__ import annotations
import json, os, math, sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bm25 import BM25, tokenize  # noqa: E402

# 원본(Kwonjunil/mirae_asset_competiton)의 중첩 경로 대신 flat 레이아웃에 맞춘 경로.
# 파일명은 기존 rag_agent.py(내 구현)의 data/vector_db/rag_embeddings_v3.json과
# 충돌하지 않도록 junil_rag_embeddings_v3.json으로 둔다.
ROOT = os.environ.get("PENSION_ROOT") or HERE
EMBEDDING_FILE = os.environ.get("RAG_EMBEDDINGS") or \
    os.path.join(ROOT, "data", "vector_db", "junil_rag_embeddings_v3.json")

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(HERE, ".env"))
API_KEY = os.getenv("CLOVASTUDIO_API_KEY")
EMBEDDING_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
RERANKER_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/reranker/v1"
TIMEOUT = float(os.environ.get("RAG_HTTP_TIMEOUT", "20"))


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


RRF_K = _envf("RAG_RRF_K", 60)
VECTOR_POOL = int(_envf("RAG_VECTOR_POOL", 40))
BM25_POOL = int(_envf("RAG_BM25_POOL", 40))

# 보너스는 RRF 점수에 곱하는 비율이다. 0.08 = +8%.
PENSION_TYPE_BONUS = _envf("RAG_BONUS_PENSION_TYPE", 0.08)
PRIMARY_TOPIC_BONUS = _envf("RAG_BONUS_TOPIC", 0.06)
ASPECT_BONUS = _envf("RAG_BONUS_ASPECT", 0.05)
HISTORICAL_PENALTY = _envf("RAG_PENALTY_HISTORICAL", 0.05)

# BM25 상위 점수 대비 이 비율 미만인 후보는 버린다 (억지로 top_k를 채우지 않기 위해)
BM25_KEEP_RATIO = _envf("RAG_BM25_KEEP_RATIO", 0.35)
# 관련도 약함 판정 (배제가 아니라 표시).
#   BM25 원점수는 질의 길이에 비례해서 절대 임계값을 쓸 수 없다.
#   ('압류' 9.3 vs '축구 국가대표 명단' 13.2 — 짧은 실제 질문이 더 낮다)
#   그래서 질의어 IDF 합으로 나눈 정규화 점수를 쓴다.
#   실측 분포: 실제 질문 0.66~2.34 / 무관 질문 0.39~1.24 — **겹친다.**
#   BM25만으로 도메인 밖 질문을 가려내는 건 신뢰할 수 없다는 뜻이다.
#   그래서 임계값을 아주 보수적으로(실제 질문 최저값보다 낮게) 잡아
#   명백한 것만 표시하고, 최종 판단은 생성 단계(근거 없으면 거절)에 맡긴다.
#   벡터 검색을 켜면 최대 코사인이 훨씬 분리력 있는 신호가 된다 → RAG_MIN_COSINE.
BM25_WEAK_NORM = _envf("RAG_BM25_WEAK_NORM", 0.5)
# 관련도 약함일 때 돌려줄 최대 후보 수. 억지로 top_k를 채우지 않는다.
WEAK_TOP_K = int(_envf("RAG_WEAK_TOP_K", 2))
MIN_COSINE = _envf("RAG_MIN_COSINE", 0.0)     # 0이면 비활성. 임베딩 켠 뒤 실측해서 조정

_WORD_RE = __import__("re").compile(r"[0-9]+(?:[.,][0-9]+)*|[A-Za-z]+|[가-힣]+")


def content_match_len(query: str, doc: str) -> int:
    """질의의 내용어(또는 그 접두어)가 문서에 실제로 나타나는 최장 길이.

    한국어는 조사가 붙어 '퇴직금을'이 문서의 '퇴직금'과 정확히 일치하지 않는다.
    그래서 접두어까지 본다.
    0이면 질의어와 문서가 **한 글자도 겹치지 않는다**는 뜻이라 도메인 밖으로 본다.
    실측: 실제 질문 최소 2 / '김치찌개 맛있게 끓이는 법' = 0.
    (다만 '축구 국가대표'는 3이 나온다 — BM25만으로는 여기까지가 한계다.
     정확한 판정은 벡터 코사인이 켜져야 가능하다.)
    """
    d = (doc or "").lower()
    best = 0
    for w in _WORD_RE.findall((query or "").lower()):
        if len(w) < 2:
            continue
        for L in range(len(w), 1, -1):
            if w[:L] in d:
                best = max(best, L)
                break
    return best


def _cosine(a, b) -> float:
    s = na = nb = 0.0
    for x, y in zip(a, b):
        s += x * y; na += x * x; nb += y * y
    d = math.sqrt(na) * math.sqrt(nb)
    return (s / d) if d else 0.0


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_list(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith("["):
        try:
            import ast
            r = ast.literal_eval(v)
            return r if isinstance(r, list) else []
        except Exception:
            return []
    return []


class PensionRAGAgent:
    """domain='pension_rule'. 상품 트랙이 붙으면 같은 클래스에 컬렉션만 추가한다."""

    def __init__(self, embedding_file: str | None = None, taxonomy=None):
        path = embedding_file or EMBEDDING_FILE
        if not os.path.exists(path):
            raise FileNotFoundError(f"임베딩 파일이 없습니다: {path}")
        with open(path, encoding="utf-8") as f:
            self.chunks: list[dict] = json.load(f)
        if not self.chunks:
            raise ValueError(f"임베딩 파일이 비어 있습니다: {path}")
        self.taxonomy = taxonomy
        self.warnings: list[str] = []

        # --- 차원 검증. 어긋나면 코사인이 조용히 이상한 값을 낸다.
        dims = {len(c["embedding"]) for c in self.chunks if c.get("embedding")}
        self.dim = next(iter(dims)) if len(dims) == 1 else None
        n_vec = sum(1 for c in self.chunks if c.get("embedding"))
        if len(dims) > 1:
            self.warnings.append(f"임베딩 차원이 섞여 있음: {sorted(dims)} — 벡터 검색 비활성화")
            for c in self.chunks:
                c.pop("embedding", None)
            self.dim, n_vec = None, 0
        elif n_vec < len(self.chunks):
            self.warnings.append(
                f"임베딩 없는 청크 {len(self.chunks) - n_vec}건 — 해당 청크는 BM25로만 검색됨")
        self.n_vec = n_vec

        # BM25는 embedding_text가 아니라 본문 + 제목으로 만든다.
        # embedding_text의 [문서]/[주제] 머리말은 모든 청크에 반복되어 노이즈다.
        self.bm25 = BM25([self._bm25_text(c) for c in self.chunks])
        self._q_emb_cache: dict[str, list[float]] = {}   # 성공한 것만 담는다

    # ------------------------------------------------------------ 유틸
    @staticmethod
    def _bm25_text(c: dict) -> str:
        parts = [c.get("title"), c.get("major_title"), c.get("sub_title"),
                 c.get("table_title"), c.get("question"), c.get("text")]
        parts += [str(s) for s in _as_list(c.get("similar_questions"))]
        return "\n".join(str(p) for p in parts if p and str(p) != "None")

    @staticmethod
    def _time_scope(c: dict) -> str | None:
        """임베딩 파일에는 time_scope와 source_time_scope가 둘 다 있다.
        한쪽만 보면 historical 감점이 조용히 죽는다."""
        for k in ("time_scope", "chunk_temporal", "source_time_scope"):
            v = c.get(k)
            if v and str(v) != "None":
                return str(v)
        return None

    # ------------------------------------------------------------ 질의 해석
    def detect_query_metadata(self, query: str) -> dict[str, list[str]]:
        if self.taxonomy is not None:
            try:
                return {
                    "pension_types": list(self.taxonomy.detect_pension_types(query)),
                    "topics": list(self.taxonomy.detect_query_topics(query)),
                    "aspects": list(self.taxonomy.detect_aspects(query)),
                }
            except Exception:
                pass          # taxonomy가 깨져도 검색 자체는 계속돼야 한다
        import re
        pts = []
        for pt, pat in [("IRP", r"IRP|개인형\s*퇴직연금"), ("DC", r"(?<![A-Za-z])DC(?![A-Za-z])|확정기여"),
                        ("DB", r"(?<![A-Za-z])DB(?![A-Za-z])|확정급여"),
                        ("연금저축계좌", r"(?<!개인)연금저축"),
                        ("디폴트옵션", r"디폴트\s*옵션|사전지정운용"),
                        ("과학기술인연금", r"과학기술인연금|과기공"),
                        ("ISA_연계", r"(?<![A-Za-z])ISA(?![A-Za-z])")]:
            if re.search(pat, query, re.I):
                pts.append(pt)
        return {"pension_types": pts, "topics": [], "aspects": []}

    # ------------------------------------------------------------ 벡터
    def embed_query(self, text: str, budget=None) -> list[float] | None:
        """성공한 임베딩만 캐시한다. 실패를 캐시하면 그 질문은 영구히 BM25 단독이 된다.

        budget이 주어지면 timeout을 남은 예산에 맞춰 줄인다. 설정값(20초)을
        그대로 쓰면 호출 하나가 전체 데드라인을 넘길 수 있다.
        """
        if text in self._q_emb_cache:
            return self._q_emb_cache[text]
        if not API_KEY or self.dim is None:
            return None
        timeout = budget.slice(TIMEOUT, floor=2.0) if budget is not None else TIMEOUT
        try:
            import requests
            r = requests.post(
                EMBEDDING_URL,
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json={"text": text}, timeout=timeout)
            r.raise_for_status()
            vec = r.json()["result"]["embedding"]
        except Exception:
            return None
        if not isinstance(vec, list) or len(vec) != self.dim:
            return None                    # 차원이 다르면 쓰지 않는다
        self._q_emb_cache[text] = vec
        return vec

    # ------------------------------------------------------------ 검색
    def search(self, query: str, top_k: int = 10,
               budget=None) -> tuple[list[dict], list[str]]:
        trace: list[str] = list(self.warnings)
        meta = self.detect_query_metadata(query)

        # --- BM25 (관련도 낮은 꼬리는 잘라낸다)
        bm_raw = self.bm25.search(query, BM25_POOL)
        weak = False
        if bm_raw:
            top = bm_raw[0][1]
            cut = top * BM25_KEEP_RATIO
            bm = [(i, s) for i, s in bm_raw if s >= cut]
            idf_sum = sum(self.bm25.idf.get(t, 0.0) for t in set(tokenize(query)))
            norm = (top / idf_sum) if idf_sum else 0.0
            weak = norm < BM25_WEAK_NORM
            trace.append(f"rag/bm25: 후보 {len(bm_raw)} → 상위 {top:.1f}의 "
                         f"{BM25_KEEP_RATIO:.0%} 미만 제외 → {len(bm)}건 "
                         f"(정규화 {norm:.2f})" + ("  ⚠ 관련도 약함" if weak else ""))
        else:
            bm = []
            trace.append("rag/bm25: 일치하는 문서 없음")

        # --- 벡터
        # 예산이 모자라면 아예 호출하지 않는다. 열화 순서: 리랭커 → 벡터 → HCX.
        # 이 순서라야 BM25 + Fact 블록이 끝까지 살아남는다.
        if budget is not None and not budget.allow_vector():
            qvec = None
            skip_why = f"예산 부족 (남은 {budget.remaining():.0f}s)"
        else:
            qvec = self.embed_query(query, budget)
            skip_why = None
        if qvec is None:
            vec_rank = []
            why = skip_why or ("키 없음" if not API_KEY else
                               ("벡터 미적재" if self.dim is None else "호출 실패"))
            trace.append(f"rag/vector: 건너뜀 ({why}) — BM25 단독")
        else:
            sims = [(i, _cosine(qvec, c["embedding"]))
                    for i, c in enumerate(self.chunks) if c.get("embedding")]
            sims.sort(key=lambda x: -x[1])
            top_cos = sims[0][1] if sims else 0.0
            if MIN_COSINE > 0 and top_cos < MIN_COSINE:
                vec_rank = []
                weak = True
                trace.append(f"rag/vector: 최대 코사인 {top_cos:.3f} < {MIN_COSINE} "
                             f"→ 도메인 밖으로 판단, 벡터 후보 제외")
            else:
                vec_rank = sims[:VECTOR_POOL]
                trace.append(f"rag/vector: 후보 {len(vec_rank)}건 "
                             f"(dim={self.dim}, 최대 코사인 {top_cos:.3f})")

        if not bm and not vec_rank:
            trace.append("rag: 근거 후보 0건 — 억지로 채우지 않음")
            return [], trace

        # --- RRF (점수가 아니라 순위로 융합한다)
        fused: dict[int, float] = {}
        for rank, (i, _) in enumerate(vec_rank, 1):
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
        for rank, (i, _) in enumerate(bm, 1):
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)

        # --- 메타데이터 보너스 (RRF 뒤. 배제가 아니라 가산점)
        scored = []
        for i, base in fused.items():
            c = self.chunks[i]
            bonus = 0.0
            if meta["pension_types"] and set(meta["pension_types"]) & set(_as_list(c.get("pension_types"))):
                bonus += PENSION_TYPE_BONUS
            if meta["topics"] and c.get("primary_topic") in meta["topics"]:
                bonus += PRIMARY_TOPIC_BONUS
            if meta["aspects"] and set(meta["aspects"]) & set(_as_list(c.get("knowledge_aspects"))):
                bonus += ASPECT_BONUS
            if self._time_scope(c) == "historical":
                bonus -= HISTORICAL_PENALTY
            scored.append((i, base * (1.0 + bonus)))

        scored.sort(key=lambda x: -x[1])
        cand = scored[: max(top_k * 3, 20)]
        trace.append(f"rag/rrf: 융합 {len(fused)}건 → 상위 {len(cand)}건 "
                     f"(보너스 배율 최대 x{1 + PENSION_TYPE_BONUS + PRIMARY_TOPIC_BONUS + ASPECT_BONUS:.2f}, "
                     f"제도={meta['pension_types']})")

        # 최상위 후보와 질의어가 한 글자도 겹치지 않으면 도메인 밖으로 본다
        best_i = cand[0][0]
        hit = content_match_len(query, self._bm25_text(self.chunks[best_i]))
        if hit == 0:
            trace.append("rag: 질의어와 최상위 문서의 내용어 겹침 0 "
                         "→ 도메인 밖으로 판단, 근거 반환하지 않음")
            return [], trace
        n_meta = (len(meta["pension_types"]) + len(meta["topics"])
                  + len(meta["aspects"]))
        if weak or n_meta == 0:
            # 도메인 사전(topic_taxonomy)에 걸리는 어휘가 하나도 없거나
            # BM25 정규화 점수가 낮으면 후보를 줄인다.
            # 실측: 도메인 밖 질문 9/9가 사전 매칭 0이지만, 도메인 안 질문도
            # 일부 0이다(ISA/TDF처럼 사전에 없는 약어). 그래서 **0건으로 만들지
            # 않고** 상한만 줄인다. 진짜 도메인 밖이면 생성 단계가 거절하고,
            # 도메인 안이면 상위 2건으로도 답할 수 있다.
            why = "관련도 약함" if weak else "도메인 사전 매칭 0"
            top_k = min(top_k, WEAK_TOP_K)
            trace.append(f"rag: {why} → 반환 상한 {top_k}건으로 축소")

        results = [self._to_evidence(self.chunks[i], s, n, weak)
                   for n, (i, s) in enumerate(cand, 1)]
        if budget is not None and not budget.allow_rerank():
            trace.append(f"rag/rerank: 건너뜀 (예산 부족, 남은 "
                         f"{budget.remaining():.0f}s) → RRF 상위 {top_k}건")
            return results[:top_k], trace
        results, rtrace = self.rerank(query, results, top_k, budget)
        trace.extend(rtrace)
        return results, trace

    # ------------------------------------------------------------ 리랭커
    def rerank(self, query: str, results: list[dict], top_k: int, budget=None):
        if not results:
            return [], ["rag/rerank: 후보 없음"]
        if not API_KEY:
            return results[:top_k], [f"rag/rerank: 건너뜀 → RRF 상위 {min(top_k, len(results))}건"]
        timeout = budget.slice(TIMEOUT, floor=2.0) if budget is not None else TIMEOUT
        try:
            import requests
            r = requests.post(
                RERANKER_URL,
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json={"query": query,
                      "documents": [{"id": e["evidence_id"], "doc": e["text"][:1500]}
                                    for e in results],
                      "topN": min(top_k, len(results))},
                timeout=timeout)
            r.raise_for_status()
            order = r.json()["result"]["documents"]
            by_id = {e["evidence_id"]: e for e in results}
            out = []
            for d in order:
                e = by_id.get(d.get("id"))
                if e:
                    e["score"] = float(d.get("score", e["score"]))
                    out.append(e)
            if not out:
                return results[:top_k], ["rag/rerank: 응답이 비어 RRF 순위 유지"]
            return out[:top_k], [f"rag/rerank: CLOVA Reranker → {len(out)}건"]
        except Exception as ex:
            return results[:top_k], [f"rag/rerank: 실패({type(ex).__name__}) → RRF 순위 유지"]

    # ------------------------------------------------------------ Evidence
    def _to_evidence(self, c: dict, score: float, idx: int, weak: bool) -> dict[str, Any]:
        caveats = []
        ts = self._time_scope(c)
        if ts == "historical":
            caveats.append("과거 제도 기준 — 현행 여부 확인 필요")
        if str(c.get("needs_chunk_review")) == "True":
            caveats.append("원문 표기 검토 대상 청크")
        if str(c.get("context_incomplete")) == "True":
            caveats.append("맥락이 잘린 청크 — 단독 인용 주의")
        if weak:
            caveats.append("질의어와 문서의 어휘 겹침이 약함 — 관련성 낮을 수 있음")
        return {
            "evidence_id": f"rag_{idx:03d}",
            "kind": "rag",
            "domain": "pension_rule",
            "text": c.get("text") or "",
            "title": c.get("title"),
            "score": float(score),
            "low_relevance": weak,
            "provenance": {
                "source_file": c.get("source_file") or c.get("file_name") or "",
                "page": _as_int(c.get("page_start")),
                "locator": c.get("source_locator") or "",
                "record_id": c.get("source_record_id") or "",
                "chunk_id": c.get("chunk_id") or "",
            },
            "quote": None,
            "confidence": c.get("topic_confidence"),
            "time_scope": ts,
            "caveats": caveats,
        }

    def run(self, question: str, top_k: int = 6, budget=None) -> dict[str, Any]:
        ev, trace = self.search(question, top_k, budget)
        return {"evidence": ev, "think_trace": trace}


if __name__ == "__main__":
    agent = PensionRAGAgent()
    for q in sys.argv[1:] or ["IRP 중도인출은 어떤 경우에 가능한가요?",
                              "디폴트옵션을 지정하지 않으면 어떻게 되나요?",
                              "김치찌개 맛있게 끓이는 법",
                              "축구 국가대표 명단 알려줘"]:
        print("=" * 74); print("Q:", q)
        out = agent.run(q, top_k=4)
        for t in out["think_trace"]:
            print("   ", t)
        for e in out["evidence"]:
            p = e["provenance"]
            print(f"    • [{e['score']:.4f}] {e['title']}  ({p['source_file']} {p['locator']})")
        if not out["evidence"]:
            print("    (근거 없음)")
