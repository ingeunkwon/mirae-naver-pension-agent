#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BM25 — 외부 의존성 없이 표준 라이브러리만 쓴다.

왜 직접 구현하는가
  팀 간에 같은 채점기·같은 검색기로 비교해야 하는데, rank_bm25 같은
  패키지는 버전에 따라 기본 파라미터가 달라진다. 표준 라이브러리만 쓰면
  누가 어디서 돌려도 같은 순위가 나온다.

한국어 토크나이저
  형태소 분석기 없이 두 가지를 함께 쓴다.
    1) 단어 토큰   — 'IRP', 'ETF', '900만원' 같은 정확 매칭용
    2) 음절 바이그램 — '중도인출' ↔ '중도 인출' 같은 띄어쓰기 흔들림 흡수용
  펀드코드·클래스코드 같은 정확 매칭이 임베딩만으로는 잘 안 잡히는데,
  BM25가 그걸 보완한다. (하이브리드를 쓰는 이유가 이것)
"""
from __future__ import annotations
import math, re
from collections import Counter, defaultdict

_WORD = re.compile(r"[0-9]+(?:[.,][0-9]+)*|[A-Za-z]+|[가-힣]+")
_HANGUL = re.compile(r"[가-힣]+")

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    toks: list[str] = []
    for w in _WORD.findall(text):
        toks.append(w)
        if _HANGUL.fullmatch(w) and len(w) >= 2:
            toks.extend(w[i:i + 2] for i in range(len(w) - 1))
    return toks


class BM25:
    def __init__(self, docs: list[str], k1: float = K1, b: float = B):
        self.k1, self.b = k1, b
        self.doc_tokens = [tokenize(d) for d in docs]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.n = len(docs)
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0

        self.tf: list[Counter] = [Counter(t) for t in self.doc_tokens]
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        # BM25+ 계열이 아닌 고전 IDF. 음수를 막기 위해 +1 형태를 쓴다.
        self.idf = {term: math.log(1 + (self.n - c + 0.5) / (c + 0.5))
                    for term, c in df.items()}
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, t in enumerate(self.tf):
            for term in t:
                self.postings[term].append(i)

    def search(self, query: str, top_k: int = 50) -> list[tuple[int, float]]:
        q = tokenize(query)
        if not q:
            return []
        scores: dict[int, float] = defaultdict(float)
        for term in set(q):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i in self.postings[term]:
                f = self.tf[i][term]
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * (f * (self.k1 + 1)) / denom
        return sorted(scores.items(), key=lambda x: -x[1])[:top_k]
