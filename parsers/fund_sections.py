# -*- coding: utf-8 -*-
"""투자설명서 섹션 분할 + 운용실적(수익률) / 설정.환매현황(시장잔고) 추출."""
import re

# 제2부.제3부 섹션 앵커. 각 섹션은 다음 앵커 직전까지다.
# 기존 파이프라인은 tax 에 종료 앵커가 없어 문서 끝(용어풀이)까지 삼켰다.
ANCHORS = [
    ('investment_objective', r'7\s*\.\s*집합투자기구의\s*투자목적'),
    ('investment_target',    r'8\s*\.\s*집합투자기구의\s*투자대상'),
    ('investment_strategy',  r'9\s*\.\s*집합투자기구의\s*투자전략'),
    ('investment_risk',      r'10\s*\.\s*집합투자기구의\s*투자위험'),
    ('purchase_redemption',  r'11\s*\.\s*매입\s*,?\s*환매'),
    ('valuation',            r'12\s*\.\s*기준가격\s*산정기준'),
    ('fees',                 r'13\s*\.\s*보수\s*및\s*수수료'),
    ('tax',                  r'14\s*\.\s*이익\s*배분\s*및\s*과세'),
    ('financials',           r'1\s*\.\s*재무정보'),
    ('subscription_history', r'2\s*\.\s*연도별\s*설정\s*및\s*환매현황'),
    ('performance',          r'3\s*\.\s*집합투자기구의\s*운용실적'),
    ('_END',                 r'제\s*4\s*부|집합투자업자에\s*관한\s*사항'),
]


def split_sections(t):
    """각 앵커의 '마지막' 등장 위치를 사용한다.
    투자설명서는 제1부(간이)와 제2부(정식)에 같은 번호 제목이 반복되기 때문이다."""
    pos = {}
    for key, pat in ANCHORS:
        hits = [m.start() for m in re.finditer(pat, t)]
        if hits:
            pos[key] = hits[-1]
    order = sorted([k for k, _ in ANCHORS if k in pos], key=lambda k: pos[k])
    out = {}
    for i, k in enumerate(order):
        s = pos[k]
        e = pos[order[i + 1]] if i + 1 < len(order) else len(t)
        if k != '_END':
            out[k] = t[s:e].strip()
    return out


PERF_LABEL = [
    ('fund',       re.compile(r'(투자신탁|투자회사|이\s*펀드|펀드전체)')),
    ('benchmark',  re.compile(r'비교지수|벤치마크|BM')),
    ('volatility', re.compile(r'수익률\s*변동성|변동성')),
]
NUM = re.compile(r'-?\d+\.\d+')


def parse_performance(perf_text):
    """'가. 연평균수익률' 표에서 (구분, 최근1년/2년/3년/5년/설정이후) 수익률을 뽑는다."""
    if not perf_text:
        return []
    # 안내문에도 '연평균 수익률', '연도별 수익률'이라는 말이 나오므로 표 제목을 앵커로.
    m = (re.search(r'가\s*\.\s*연평균\s*수익률', perf_text)
         or re.search(r'연평균\s*수익률\s*\(\s*단위', perf_text)
         or re.search(r'연평균\s*수익률', perf_text))
    blk = perf_text[m.end():] if m else perf_text
    end = re.search(r'나\s*\.\s*연도별', blk) or re.search(r'나\s*\.', blk)
    if end:
        blk = blk[:end.start()]
    out, prev = [], 0
    for m in re.finditer(r'(?:[ \t]*-?\d+\.\d+[ \t\n]*){3,}', blk):
        label = re.sub(r'\s+', '', blk[prev:m.start()])
        prev = m.end()
        vals = [float(x) for x in NUM.findall(m.group(0))]
        if not (3 <= len(vals) <= 6):
            continue
        kind = next((k for k, p in PERF_LABEL if p.search(label[-30:])), None)
        cname = None
        if kind is None:
            mm = (re.search(r'종류\s*([A-Za-z][A-Za-z0-9\-]*)', label)
                  or re.search(r'Class\s*([A-Za-z][A-Za-z0-9\-]*)', label, re.I)
                  or re.search(r'\(([A-Za-z][A-Za-z0-9\-]{0,8})\)\s*$', label))
            if mm:
                kind, cname = 'class', mm.group(1).upper()
        if kind is None:
            continue
        for p_, v in zip(['1y', '2y', '3y', '5y', 'since'][:len(vals)], vals):
            out.append(dict(kind=kind, class_name=cname, period=p_, value=v))
    return out


INT = re.compile(r'[\d][\d,]*|-')
# 날짜 표기(2024.07.08 / '22.05.11~)는 숫자로 오인되므로 먼저 제거한다
DATEPAT = re.compile(r"'?\d{2,4}\s*[.\-/]\s*\d{1,2}\s*[.\-/]\s*\d{1,2}\s*~?")
FOOTNOTE = re.compile(r'^\s*주\s*\d*\s*\)')
# 표 단위 표기: [단위:백만] / [단위:억좌, 억원] / (단위:천원) 등
UNITPAT = re.compile(r'단\s*위\s*[:：]?\s*([^\]\)\n]{0,20})')
UNIT_TO_MILLION = {'억': 100.0, '백만': 1.0, '천': 0.001, '원': 0.000001}


def _unit_factor(text):
    """표에 적힌 금액 단위를 '백만원' 기준 배수로 바꾼다. 못 찾으면 None.

    표기가 제각각이다: '백만' / '억좌, 억원' / '백만좌,백만원' / '천원'.
    게다가 '단위:%' 처럼 금액과 무관한 표기가 먼저 나오는 문서가 있어,
    섹션 전체를 훑되 금액 단위 키워드가 들어간 첫 표기를 채택한다.
    """
    for m in UNITPAT.finditer(text):
        u = m.group(1)
        for key in ('백만', '억', '천', '원'):
            if key in u:
                return UNIT_TO_MILLION[key]
    return None


def parse_aum(sub_text):
    """'2. 연도별 설정 및 환매현황' 표에서 클래스별 최근 기간말 잔고를 뽑아
    백만원 단위로 정규화한다.

    행 구성(9열): 기간초(좌수,금액) 설정(좌수,금액) 환매(좌수,금액) 기간말(좌수,금액) 재투자금액

    주의해야 할 함정이 세 가지 있다.
      1) 기간 칸의 날짜(2024.07.08)가 숫자로 읽혀 열 위치가 밀린다  -> 날짜를 먼저 지운다
      2) '주 1) ... 재투자 좌수/금액 : 2,110,803,557 좌' 같은 각주가 데이터 행으로 잡힌다
                                                        -> 각주 줄을 건너뛴다
      3) 단위가 펀드마다 다르다(백만 / 억원 / 천원)        -> 헤더의 단위 표기로 환산한다
    """
    if not sub_text:
        return []
    factor = _unit_factor(sub_text)
    if factor is None:
        return []                      # 단위를 확인할 수 없으면 수치를 만들지 않는다
    # 클래스별로 표를 나눠 쓰는 문서가 있고, 펀드 전체 한 표만 싣는 문서도 있다.
    # 후자는 클래스 행이 아예 없으므로 '_FUND'(펀드 합계)로 잡는다.
    cur, last = '_FUND', {}
    for ln in sub_text.split('\n'):
        if FOOTNOTE.match(ln):
            continue
        mc = re.search(r'종류\s*([A-Za-z][A-Za-z0-9\-]*)', ln)
        if mc:
            cur = mc.group(1).upper()
            continue
        mc2 = re.match(r'\s*\(?([A-Z][A-Za-z0-9\-]{0,8})\)?\s*$', ln.strip())
        if mc2 and not re.search(r'\d', ln):
            cur = mc2.group(1).upper()
            continue
        toks = [t for t in INT.findall(DATEPAT.sub(' ', ln)) if t != '-']
        if not (8 <= len(toks) <= 9):   # 정규 행이 아니면 버린다
            continue
        try:
            vals = [int(t.replace(',', '')) for t in toks]
        except ValueError:
            continue
        units, amount = vals[6], vals[7]        # 기간말 좌수 / 기간말 금액
        if units <= 0 or amount <= 0:
            continue
        if not (0.1 <= amount / units <= 20):   # 좌수와 금액이 같은 축이 아니면 열이 밀린 것
            continue
        last[cur] = int(round(amount * factor))
    return [dict(class_name=k, end_balance_million=v) for k, v in last.items() if v]
