"""institution_sql_agent / institution_rag_agent 결과를 orchestrator.py가 쓰는
rag_context / sql_context 텍스트 블록으로 직렬화한다.

[확정 수치] / [요건 판정 보류] / [계산 보류] 라벨은 팀원(Kwonjunil)의
mirae_asset_competiton 저장소 orchestrator.compose()가 쓰던 규약을 그대로 가져온
것이다. 생성 모델이 "요건 미확정"인 값을 "충족한다"고 단정하지 않도록 유도하는
문구라, 우리 쪽 synthesize_answer 프롬프트("근거에 적힌 값을 변형 없이 인용한다")
와도 그대로 맞물린다. 이 파일은 새 텍스트를 만들지 않고 institution_sql_agent /
institution_rag_agent가 이미 계산한 evidence·caveat·requirement_status를 그대로
옮겨 적을 뿐이다 — 판정 로직은 건드리지 않는다.
"""
from __future__ import annotations
import re

from institution_sql_agent import run_formula, CALC_OK
from tri import UNKNOWN


def run_institution_sql(agent, question: str, limit: int = 12):
    """institution_sql_agent.PensionSQLAgent.run() + calc까지 실행해
    (sql_context 텍스트, think_trace 목록) 을 돌려준다.

    calc 우선순위 로직은 Kwonjunil 원본 orchestrator.py의 5번 노드(calc)를
    그대로 옮긴 것이다 — 후보 공식 중 계산 가능한 첫 번째를 채택하고,
    전부 막히면 가장 설명력 있는 차단 사유를 [계산 보류] 블록으로 남긴다.
    """
    result = agent.run(question, limit=limit)
    facts = list(result.get("evidence") or [])
    formulas = result.get("formulas") or []
    given = (result.get("detected") or {}).get("conditions") or {}
    trace = list(result.get("think_trace") or [])

    calc = None
    for fm in formulas:
        res = run_formula(fm, given, facts)
        if res.status == CALC_OK:
            facts.insert(0, res.evidence)
            trace.append(f"calc: {fm['name']} 계산 (코드 직접 계산, LLM 미사용)")
            calc = res
            break
        if calc is None or (calc.status == "insufficient_input" and res.blocked()):
            calc = res
    if calc is not None and calc.status != CALC_OK:
        trace.append(f"calc: {calc.status} — {calc.reason} "
                     f"(계산값을 [확정 수치]에 넣지 않음, 후보 {len(formulas)}개)")

    text = format_institution_sql_context(facts, calc)
    return text, trace


def format_institution_sql_context(facts: list, calc=None) -> str:
    if not facts and (calc is None or not calc.blocked()):
        return ""
    parts = []
    if facts:
        parts.append("[확정 수치 — 숫자는 반드시 이 블록에서만 인용할 것]")
        for f in facts:
            p = f.get("provenance") or {}
            parts.append(f"- {f.get('text')}")
            if f.get("quote"):
                parts.append(f'  원문: "{f.get("quote")}"')
            parts.append(f"  출처: {p.get('source_file')} {p.get('locator') or ''}")
            for c in f.get("caveats", []):
                parts.append(f"  주의: {c}")
        parts.append("")

    pend = [f for f in facts if f.get("requirement_status") == UNKNOWN]
    if pend:
        parts.append("[요건 판정 보류 — 충족한다고 단정하지 말 것]")
        for f in pend:
            labels = f.get("unknown_condition_labels") or f.get("unknown_conditions") or []
            parts.append(f"- {f.get('item')}: 다음 정보가 없어 충족 여부 미확정 — "
                         f"{', '.join(str(x) for x in labels)}")
        parts.append("→ 답변에서 이 요건들을 '충족한다'고 쓰지 말고, 위 정보를 "
                     "확인 질문으로 요청할 것.")
        parts.append("")

    if calc is not None and calc.blocked():
        fm = calc.formula or {}
        parts.append("[계산 보류 — 이 금액은 산출하지 않았습니다. "
                     "임의로 계산해 넣지 마십시오]")
        if calc.status == "blocked_unmet":
            parts.append(f"'{fm.get('name', '해당 계산')}'은 요건을 충족하지 않아 "
                         f"금액을 산출하지 않았습니다.")
            for b in calc.blockers:
                parts.append(f"  · 충족하지 못한 요건: {b}")
        else:
            parts.append(f"'{fm.get('name', '해당 계산')}'은 계산에 필요한 정보가 "
                         f"부족하거나 요건을 확정할 수 없어 금액을 산출하지 않았습니다.")
            ask = calc.ask_for()
            if ask:
                parts.append("  · 다음 정보를 알려주시면 계산해 드릴 수 있습니다: "
                             + ", ".join(ask))
        if fm.get("expression"):
            parts.append(f"  · 참고 산식: {fm['expression']}")
        parts.append("")

    return "\n".join(parts).strip()


def format_institution_rag_context(evidence: list) -> str:
    if not evidence:
        return ""
    parts = ["[제도 및 약관 문서 근거 — 제도 RAG(BM25+벡터 RRF)]"]
    for e in evidence:
        p = e.get("provenance") or {}
        body = re.sub(r"\s+", " ", e.get("text") or "").strip()
        title = e.get("title")
        head = f"({p.get('source_file')} {p.get('locator') or ''})"
        parts.append(f"- {head} {title + ': ' if title else ''}{body}")
        for c in e.get("caveats", []):
            parts.append(f"  주의: {c}")
    return "\n".join(parts)
