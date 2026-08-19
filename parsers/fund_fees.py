# -*- coding: utf-8 -*-
"""투자설명서 '집합투자기구에 부과되는 보수 및 비용' 표 파서.

핵심 아이디어
  총보수 = 집합투자업자보수 + 판매회사보수 + 신탁업자보수 + 일반사무관리회사보수
  이 항등식으로 '몇 번째 숫자가 총보수인지'를 자기검증한다. 운용사마다 컬럼 구성과
  개수가 달라 헤더 문자열 매칭은 신뢰할 수 없기 때문이다.

두 가지 표 레이아웃을 모두 지원한다.
  (1) 행=클래스 : 클래스별로 한 줄에 보수 항목이 나열되는 일반형
  (2) 열=클래스 : 보수 항목이 행이고 클래스가 열인 전치형
"""
import re

NUMRUN  = re.compile(r'(?:[ \t]*(?:\d+\.\d+|-)[ \t\n]*){5,}')
H_SEC13 = re.compile(r'13\s*\.\s*보수\s*및\s*수수료')
H_TBL   = re.compile(r'집합투자기구에\s*부과되는\s*보수\s*및\s*비용')
H_RATE  = re.compile(r'지급비율|부과비율|지급비용')

ROW_LABELS = [
    ('mgmt',  re.compile(r'집합\s*투자\s*업\s*자?\s*보\s*수')),
    ('sales', re.compile(r'판매\s*회사\s*보\s*수')),
    ('trust', re.compile(r'(?:신탁\s*(?:업자|회사)|수탁\s*회사)\s*보\s*수')),
    ('admin', re.compile(r'일반\s*사무\s*관리?\s*회?사?\s*보\s*수')),
    ('total', re.compile(r'^\s*총\s*보\s*수\s*$')),
]

CLS = [re.compile(r'종류\s*([A-Za-z][A-Za-z0-9\-]*)\s*$'),
       re.compile(r'\(\s*([A-Za-z][A-Za-z0-9\-]*)\s*\)\s*$'),
       re.compile(r'([A-Z][A-Za-z0-9]*-[가-힣]{1,8})$'),
       re.compile(r'(?<![(\-])([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]{1,6})*)$'),
       re.compile(r'종류([A-Za-z][A-Za-z0-9\-]*)'),
       re.compile(r'\(([A-Za-z][A-Za-z0-9\-]*)\)')]
BAD = {'AUM', 'ETF', 'KRX', 'MP', 'TDF', 'ELS', 'MMF', 'CD', 'KIS'}
KOR_CLS = re.compile(r'(투자신탁|모투자신탁|단일클래스)$')
CLS_PRE  = CLS[0:1] + CLS[4:5] + CLS[1:4]
CLS_POST = CLS[5:6]


def fee_block(t):
    """정식 투자설명서(제2부)의 보수표 블록만 잘라낸다.

    - 간이투자설명서에 같은 표가 요약 기재되므로 표 헤더(지급비율 등)가 뒤따르는
      마지막 제목을 사용한다.
    - 제목 문구가 운용사마다 달라 제목을 못 찾으면 표 헤더 자체를 시작점으로 삼는다.
    """
    cands = [m.end() for m in H_TBL.finditer(t) if H_RATE.search(t, m.end(), m.end() + 400)]
    if not cands:
        cands = [m.start() for m in H_RATE.finditer(t)]
    if not cands:
        return None
    s = cands[-1]
    e = len(t)
    m14 = re.search(r'14\s*\.\s*이익\s*배분', t[s:])
    if m14:
        e = s + m14.start()
    for pat in (re.compile(r'지급시기'), re.compile(r'부과시기')):
        for mm in pat.finditer(t, s):
            if mm.start() - s > 1500:      # 표 헤더의 '지급/시기' 열 제목에 걸리지 않도록
                e = min(e, mm.start()); break
    e = min(e, s + 15000)
    return t[s:e].replace('없음', ' 0.000 ')   # '일반사무관리보수 없음' -> 0


def _clean(s):
    return re.sub(r'\s+', '', s)


def _pick(label, pats):
    for p in pats:
        m = p.search(label)
        if m and len(m.group(1)) <= 12 and m.group(1).upper() not in BAD:
            return m.group(1).upper()
    return None


def _class_of(label):
    m = KOR_CLS.search(label)
    if m:
        return m.group(1)
    return _pick(label, CLS)


def _class_pre(label):
    m = KOR_CLS.search(label)
    if m:
        return m.group(1)
    return _pick(label, CLS_PRE)


def _class_post(label):
    return _pick(label, CLS_POST)


def _trim(vals):
    while vals and vals[0] is None:
        vals = vals[1:]
    while vals and vals[-1] is None:
        vals = vals[:-1]
    return vals


def _row(vals):
    """숫자열에서 (mgmt, sales, trust, admin, total, etc, total_cost) 위치를 확정."""
    vals = _trim(list(vals))
    if len(vals) < 5:
        return None
    head = [0.0 if v is None else v for v in vals[:4]]   # '-' 는 해당 없음 -> 0
    s4 = round(sum(head), 4)
    ti = next((k for k in range(4, min(len(vals), 8))
               if vals[k] is not None and abs(vals[k] - s4) < 0.006), None)
    if ti is not None:
        vals = head + list(vals[4:])
    if ti is None:
        # 보조 규칙: 총보수 + 기타비용 = 총보수.비용 (셀 병합으로 항목 일부가 비는 표)
        for k in range(1, len(vals) - 2):
            a, b, c = vals[k], vals[k + 1], vals[k + 2]
            if None in (a, b, c) or a <= 0:
                continue
            if abs((a + b) - c) < 0.0006 and b < a:
                ti = k
                break
        if ti is None:
            return None
        return dict(mgmt_fee=None, sales_fee=None, trust_fee=None, admin_fee=None,
                    total_fee=vals[ti], etc_cost=vals[ti + 1], total_cost=vals[ti + 2])
    etc = vals[ti + 1] if ti + 1 < len(vals) else None
    tc = vals[ti + 2] if ti + 2 < len(vals) else None
    if etc is not None and tc is not None and abs((vals[ti] + etc) - tc) > 0.006:
        etc = tc = None
    return dict(mgmt_fee=vals[0], sales_fee=vals[1], trust_fee=vals[2],
                admin_fee=vals[3], total_fee=vals[ti], etc_cost=etc, total_cost=tc)


def _parse_rowwise(blk):
    runs = [m for m in NUMRUN.finditer(blk) if len(re.findall(r'\d+\.\d+', m.group(0))) >= 4]
    out = []
    for idx, m in enumerate(runs):
        prev_end = runs[idx - 1].end() if idx else 0
        next_start = runs[idx + 1].start() if idx + 1 < len(runs) else len(blk)
        before = _clean(blk[prev_end:m.start()])
        vals = [None if x == '-' else float(x) for x in re.findall(r'\d+\.\d+|-', m.group(0))]
        r = _row(vals)
        if not r:
            continue
        # 클래스명 위치는 운용사마다 다르다(숫자 앞 / 숫자 뒤 / 줄머리).
        after_txt = blk[m.end():next_start]
        c = None
        mb = re.search(r'\(([A-Za-z0-9\-]*)$', before)   # '(C-' + 'e)' 로 쪼개진 이름 결합
        if mb:
            ma = re.match(r'([A-Za-z0-9\-]*)\)', _clean(after_txt))
            if ma:
                cand = (mb.group(1) + ma.group(1)).upper()
                if cand and cand not in BAD and len(cand) <= 12:
                    c = cand
        if not c:
            c = _class_pre(before)
        if not c:
            for ln in after_txt.split('\n'):
                if ln.strip():
                    c = _class_of(_clean(ln))
                    if c:
                        break
        if not c:
            c = _class_post(before)
        if not c:
            continue
        out.append(dict(class_name=c, class_desc=before[-60:], **r))
    return out


def _parse_columnwise(blk):
    """전치형: 보수 항목이 행, 클래스가 열. 라벨이 숫자 줄 위아래로 쪼개지므로
    각 숫자 줄의 앞뒤 2줄을 합쳐 라벨을 판정한다."""
    lines = blk.split('\n')

    def nums(s):
        return [None if x == '-' else float(x) for x in re.findall(r'\d+\.\d+|-', s)]

    def words(s):
        return re.sub(r'[\d\.\-\s]', '', s)

    buckets = {}
    first_num_line = None
    for i, ln in enumerate(lines):
        if len(re.findall(r'\d+\.\d+', ln)) < 3:
            continue
        v = nums(ln)
        if first_num_line is None:
            first_num_line = i
        ctx = ''.join(words(lines[j]) for j in range(max(0, i - 2), min(len(lines), i + 3)))
        for key, pat in ROW_LABELS:
            if key == 'total':
                continue
            if pat.search(ctx) and key not in buckets:
                buckets[key] = v
                break
    need = ['mgmt', 'sales', 'trust', 'admin']
    if not all(k in buckets for k in need):
        # 라벨이 줄바꿈으로 깨진 경우: 보수 항목은 항상
        # 집합투자업자 -> 판매회사 -> 신탁업자 -> 일반사무관리 -> 총보수 순서라는 점 이용
        rows = [nums(ln) for ln in lines if len(re.findall(r'\d+\.\d+', ln)) >= 3]
        if len(rows) < 5:
            return []
        width = max(set(len(r) for r in rows), key=[len(r) for r in rows].count)
        rows = [r for r in rows if len(r) == width][:5]
        if len(rows) < 5:
            return []
        sums = [round(sum(rows[k][j] for k in range(4)), 4)
                if all(rows[k][j] is not None for k in range(4)) else None
                for j in range(width)]
        okc = sum(1 for j in range(width)
                  if sums[j] is not None and rows[4][j] is not None
                  and abs(rows[4][j] - sums[j]) < 0.006)
        if okc < max(1, width // 2):      # 5번째 행이 총보수임을 과반 검증
            return []
        buckets = dict(zip(need, rows[:4]))
    n = min(len(buckets[k]) for k in need)
    if n == 0:
        return []
    names = []
    for ln in lines[:first_num_line or 0]:
        for m in re.finditer(r'(?<![가-힣A-Za-z0-9\-])([A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]{1,4})?)(?![가-힣])', ln):
            g = m.group(1).upper()
            if g not in BAD and len(g) <= 12 and g not in names:
                names.append(g)
    out = []
    for j in range(n):
        vals = [buckets[k][j] for k in need]
        if any(v is None for v in vals):
            continue
        out.append(dict(class_name=names[j] if j < len(names) else 'CLASS%d' % (j + 1),
                        class_desc='', mgmt_fee=vals[0], sales_fee=vals[1],
                        trust_fee=vals[2], admin_fee=vals[3],
                        total_fee=round(sum(vals), 4), etc_cost=None, total_cost=None))
    return out


def parse_fees(text):
    blk = fee_block(text)
    if not blk:
        return []
    rows = _parse_rowwise(blk) or _parse_columnwise(blk)
    seen, out = set(), []
    for r in rows:
        k = (r['class_name'], r['total_fee'])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out
