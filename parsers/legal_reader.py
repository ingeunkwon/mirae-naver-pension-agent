# -*- coding: utf-8 -*-
"""안내문 PDF 말미의 '첨부 - 관련법령/행정해석/판례' 구간을 조문 단위로 추출한다.

기존 QA 파서는 '질문' 패턴만 훑고 첨부 구간을 버려서, 답변에 붙일 법적 근거가
지식베이스에 하나도 남지 않았다.
"""
import re

START = re.compile(r'첨\s*부|관련\s*법령')
ART = re.compile(
    r'((?:[가-힣]+\s*){1,5}(?:법률|법|규정|규칙|시행령|시행규칙)'
    r'\s*제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*제\s*\d+\s*항)?[^\n]{0,50})')


def extract_legal(text):
    """[{law_title, text}] 리스트."""
    hits_start = [mm.start() for mm in START.finditer(text)]
    if not hits_start:
        return []
    body = text[hits_start[-1]:]          # 마지막 '첨부/관련법령' 이후가 법령 구간
    hits = list(ART.finditer(body))
    if not hits:
        return []
    out = []
    for i, h in enumerate(hits):
        s = h.start()
        e = hits[i + 1].start() if i + 1 < len(hits) else len(body)
        chunk = re.sub(r'\n{2,}', '\n', body[s:e]).strip()
        chunk = re.sub(r'\n?\s*\d+\s*/\s*\d+\s*Mirae Asset Securities\s*', '', chunk)
        if len(re.sub(r'\s+', '', chunk)) < 40:
            continue
        title = re.sub(r'\s+', ' ', h.group(1)).strip()
        title = re.sub(r'^(?:판례\s*등|행정해석|관련\s*법령)\s*', '', title)
        out.append({'law_title': title, 'text': chunk})
    return out
