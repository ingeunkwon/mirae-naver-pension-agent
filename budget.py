#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Budget — 남은 시간을 노드 사이가 아니라 **네트워크 호출 하나하나까지** 내려보낸다.

왜 필요한가
  v3의 데드라인은 노드 **사이**에서만 확인했다. 그래서 단일 HCX 호출이나
  임베딩 호출 하나가 남은 시간을 통째로 넘겨도 막을 방법이 없었다.
      remaining()이 200초라 통과 → HCX가 40초 timeout으로 걸림 → 다음 노드에서야 발견

  Budget은 호출 직전에 `slice()`로 그 호출이 쓸 수 있는 실제 timeout을 계산한다.

설계 포인트 3개
  1. 하향 전달. Orchestrator가 만들어 RAG·HCX까지 같은 객체를 넘긴다.
  2. reserve를 남긴다. 마지막에 근거 요약 fallback을 만들 몫(기본 3초)이다.
     전액을 검색에 쓰면 **시간은 다 쓰고 답은 못 만드는** 최악이 된다.
  3. 열화 순서를 고정한다 (degrade_level).
        예산 충분  → 리랭커 + 벡터 + HCX
        줄어들면   → 리랭커 끔
        더 줄면    → 벡터 끔 (BM25 단독)
        더 줄면    → HCX 끔 (근거 요약 fallback)
     이 순서면 BM25 + Fact 블록은 끝까지 살아남는다.

예산 소진은 예외가 아니라 **정상 경로**다. 어느 단계로 열화했는지는
think_trace에 남는다. (API 키가 없을 때 BM25로 빠지는 것과 같은 패턴)
"""
from __future__ import annotations
import time

# 각 단계를 켜기 위해 최소한 남아 있어야 하는 시간(초).
NEED_RERANK = 25.0
NEED_VECTOR = 15.0
NEED_COMPOSE = 8.0
DEFAULT_RESERVE = 3.0


class Budget:
    """남은 시간 계산기. 스레드 간 공유하지 않는다 (요청 1건당 1개)."""

    __slots__ = ("t0", "total", "reserve", "_notes")

    def __init__(self, total: float, reserve: float = DEFAULT_RESERVE, t0: float | None = None):
        self.total = float(total)
        self.reserve = float(reserve)
        self.t0 = time.time() if t0 is None else t0
        self._notes: list[str] = []

    # ------------------------------------------------------------------
    def elapsed(self) -> float:
        return time.time() - self.t0

    def remaining(self) -> float:
        return self.total - self.elapsed()

    def spendable(self) -> float:
        """fallback 몫을 뺀, 지금 실제로 쓸 수 있는 시간."""
        return self.remaining() - self.reserve

    def slice(self, want: float, floor: float = 1.0) -> float:
        """이 호출에 걸 timeout. 설정값과 남은 예산 중 작은 쪽.

        floor 아래로는 내려가지 않는다. 0이나 음수를 timeout으로 넘기면
        requests가 즉시 예외를 내는데, 그건 '건너뛰기'와 의미가 다르다.
        건너뛸지 말지는 allow_*()로 **먼저** 판단한다.
        """
        return max(floor, min(float(want), self.spendable()))

    # ---------------------------------------------------------- 열화 판단
    def allow_rerank(self) -> bool:
        return self.remaining() >= NEED_RERANK

    def allow_vector(self) -> bool:
        return self.remaining() >= NEED_VECTOR

    def allow_compose(self) -> bool:
        return self.remaining() >= NEED_COMPOSE

    def degrade_level(self) -> str:
        if self.allow_rerank():
            return "full"
        if self.allow_vector():
            return "no_rerank"
        if self.allow_compose():
            return "bm25_only"
        return "fallback_only"

    # ------------------------------------------------------------------
    def note(self, msg: str) -> None:
        self._notes.append(f"budget: {msg} (남은 {self.remaining():.0f}s)")

    def notes(self) -> list[str]:
        return list(self._notes)

    def __repr__(self):
        return (f"Budget(total={self.total:.0f}s, 남은={self.remaining():.0f}s, "
                f"level={self.degrade_level()})")


class NullBudget(Budget):
    """예산 제한 없이 돌릴 때 (단위 테스트, CLI 단독 실행)."""

    def __init__(self):
        super().__init__(total=1e9, reserve=0.0)
