# -*- coding: utf-8 -*-
"""투자설명서 100건 재파싱  ->  product_profiles_v2.json / product_sections_v2.json
                                data/fund_prospectus_v2.sqlite

기존 파이프라인 대비 바뀐 점
  1) 섹션 경계에 종료 앵커를 넣어 tax 섹션이 문서 끝까지 삼키던 문제를 없앴다.
  2) 제3부(재무정보 / 연도별 설정.환매현황 / 운용실적)를 별도 섹션으로 분리해
     수익률과 시장잔고를 되살렸다.
  3) 보수표 파서를 항등식 자기검증 방식으로 새로 썼다 (fund_class_fees 0행 -> 복구).
  4) 운용사명을 정규화하고, 같은 투자설명서를 공유하는 클래스를 master_fund_id 로 묶는다.
"""
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from parsers.fund_fees import parse_fees
from parsers.fund_sections import ANCHORS, parse_aum, parse_performance, split_sections
from parsers.pdf_text import extract_text, tidy

def _find_src_dir():
    """투자설명서 폴더를 찾는다.

    폴더명이 유니코드 NFD(자모 분리) 로 저장된 경우가 있어(맥에서 압축한 자료 등)
    이름을 문자열로 비교하면 못 찾는다. R2_*.pdf 를 품은 폴더를 직접 탐색한다.
    """
    cand = os.path.join(BASE_DIR, '투자설명서')
    if glob.glob(os.path.join(cand, '*', 'R2_*.pdf')):
        return cand
    for d in sorted(os.listdir(BASE_DIR)):
        full = os.path.join(BASE_DIR, d)
        if os.path.isdir(full) and glob.glob(os.path.join(full, '*', 'R2_*.pdf')):
            return full
    return cand


SRC_DIR = _find_src_dir()
OUT_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(OUT_DIR, 'fund_prospectus_v2.sqlite')
PROFILES_JSON = os.path.join(BASE_DIR, 'product_profiles_v2.json')
SECTIONS_JSON = os.path.join(BASE_DIR, 'product_sections_v2.json')

SECTION_KEYS = [k for k, _ in ANCHORS if k != '_END']
CHUNK = 1200

RISK = re.compile(r'([1-6])\s*등급\s*[\[(]\s*([^\])]+?)\s*[\])]')
RISK2 = re.compile(r'([1-6])\s*등급\s*으?로\s*분류')
RISK_LABEL = {1: '매우 높은 위험', 2: '높은 위험', 3: '다소 높은 위험',
              4: '보통 위험', 5: '낮은 위험', 6: '매우 낮은 위험'}
DATE2 = re.compile(r'[(\[]\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*기준')
PTYPE2 = re.compile(r'종류\s*및\s*형태[^\n]{0,120}')
NAME = [re.compile(r'1\s*\.\s*집합투자기구\s*명칭\s+(\S[^\n]*)'),
        re.compile(r'이\s*투자설명서는\s*(\S[^\n]*?)에\s*대한')]
MGR = re.compile(r'2\s*\.\s*집합투자업자\s*명칭\s+(\S[^\n]*)')
MGR2 = re.compile(r'([가-힣A-Za-z\-]+\s*자산운용(?:\s*주식회사|㈜|\(주\))?)')
DATE = re.compile(r'작성\s*기준일\s*[:\s]\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일')
PTYPE = re.compile(r'(주식-파생형|채권-파생형|채권혼합형|주식혼합형|재간접형|주식형|채권형|혼합형|파생형|MMF)')
PTYPE_HDR = re.compile(r'종류\s*및\s*형태[^\n]{0,150}')
# 상품명 끝 괄호 표기: '...자투자신탁1호(채권)' / '[채권]' / '(주식-파생재간접형)'
PTYPE_NAME = re.compile(r'[\(\[]\s*(주식|채권|혼합|재간접|파생|단기금융|MMF)[^\)\]]*[\)\]]\s*$')
PTYPE_MAP = {'주식': '주식형', '채권': '채권형', '혼합': '혼합형',
             '재간접': '재간접형', '파생': '파생형', '단기금융': 'MMF', 'MMF': 'MMF'}


def detect_product_type(name, tidy_text):
    """상품 유형(자산유형) 판정.

    본문 앞부분만 훑으면 추출기(pdftotext / pdfplumber)에 따라 앞 N자에 담기는
    실제 내용이 달라져 실패한다. 상품명 끝의 (채권)/(주식) 표기를 우선 쓰면
    추출기와 무관하게 안정적이다.
    """
    hdr = PTYPE_HDR.search(tidy_text)
    if hdr:
        m = PTYPE.search(hdr.group(0))
        if m:
            return m.group(1)
    if name:
        m = PTYPE.search(name)
        if m:
            return m.group(1)
        m = PTYPE_NAME.search(name)
        if m:
            return PTYPE_MAP.get(m.group(1))
    m = PTYPE.search(tidy_text[:30000])
    return m.group(1) if m else None


def clean_mgr(s):
    """'의 명칭 : 우리자산운용(주)', '베어링자산운용(주)  4.' 같은 파싱 찌꺼기를 정규화."""
    if not s:
        return None
    s = re.sub(r'\s+', ' ', s).strip()
    m = MGR2.search(s)
    if m:
        s = m.group(1)
    s = re.sub(r'\s+', '', s)
    s = s.replace('주식회사', '').replace('(주)', '').replace('㈜', '')
    s = re.sub(r'^\(?주\)?', '', s)
    return s or None


def meta_of(text, code):
    name = None
    for p in NAME:
        m = p.search(text)
        if m:
            name = re.sub(r'\s+', ' ', m.group(1)).strip()
            break
    m = MGR.search(text)
    mgr = clean_mgr(m.group(1) if m else None) or clean_mgr(text[:4000])
    if name:
        name = re.sub(r'^[\s:：·\-]+', '', name).strip()
    grade = None
    m = RISK.search(text) or RISK2.search(text)
    if m:
        grade = int(m.group(1))
    label = RISK_LABEL.get(grade)          # 표기 흔들림(높은위험/높은 위험) 정규화
    m = DATE.search(text) or DATE2.search(text)
    date = '%s-%02d-%02d' % (m.group(1), int(m.group(2)), int(m.group(3))) if m else None
    return dict(product_code=code, product_name=name, asset_manager=mgr,
                document_date=date, risk_grade=grade, risk_label=label,
                product_type=detect_product_type(name, tidy(text)))


def chunks_of(txt, size=CHUNK):
    txt = re.sub(r'\n{3,}', '\n\n', txt.strip())
    out, buf = [], []
    for para in txt.split('\n'):
        if sum(len(x) for x in buf) + len(para) > size and buf:
            out.append('\n'.join(buf))
            buf = []
        buf.append(para)
    if buf:
        out.append('\n'.join(buf))
    return [c for c in out if re.sub(r'\s+', '', c)]


def init_db(conn):
    c = conn.cursor()
    c.executescript("""
    DROP TABLE IF EXISTS fund_products;
    DROP TABLE IF EXISTS fund_profiles;
    DROP TABLE IF EXISTS fund_class_fees;
    DROP TABLE IF EXISTS fund_performance;
    DROP TABLE IF EXISTS fund_aum;
    DROP TABLE IF EXISTS fund_sections;

    CREATE TABLE fund_products (
        product_code   TEXT PRIMARY KEY,
        product_name   TEXT NOT NULL,
        asset_manager  TEXT,
        document_date  TEXT,
        risk_grade     INTEGER,
        risk_label     TEXT,
        product_type   TEXT,
        master_fund_id TEXT,
        source_file    TEXT
    );
    CREATE TABLE fund_profiles (
        product_code TEXT PRIMARY KEY,
        investment_objective TEXT, investment_target TEXT, investment_strategy TEXT,
        investment_risk TEXT, purchase_redemption TEXT, valuation TEXT,
        fees TEXT, tax TEXT, financials TEXT,
        subscription_history TEXT, performance TEXT
    );
    CREATE TABLE fund_class_fees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT, class_name TEXT, class_desc TEXT,
        mgmt_fee REAL, sales_fee REAL, trust_fee REAL, admin_fee REAL,
        total_fee REAL, etc_cost REAL, total_cost REAL
    );
    CREATE TABLE fund_performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT, kind TEXT, class_name TEXT, period TEXT, value REAL
    );
    CREATE TABLE fund_aum (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_code TEXT, class_name TEXT, end_balance_million INTEGER
    );
    CREATE TABLE fund_sections (
        record_id TEXT PRIMARY KEY,
        product_code TEXT, product_name TEXT, section TEXT, chunk_no INTEGER,
        text TEXT, search_text TEXT, source_file TEXT
    );
    CREATE INDEX idx_fee_code  ON fund_class_fees(product_code);
    CREATE INDEX idx_fee_total ON fund_class_fees(total_fee);
    CREATE INDEX idx_perf_code ON fund_performance(product_code);
    CREATE INDEX idx_sec_code  ON fund_sections(product_code);
    """)
    conn.commit()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    pdfs = sorted(glob.glob(os.path.join(SRC_DIR, '*', 'R2_*.pdf')))
    if not pdfs:
        print('원본을 찾을 수 없습니다:', SRC_DIR)
        return

    profiles, sections = [], []
    fees, perfs, aums = [], [], []
    hash2master = {}
    stat = dict(fee_ok=0, perf_ok=0, aum_ok=0)

    for path in pdfs:
        code = os.path.basename(path)[3:-4]
        text = extract_text(path)
        meta = meta_of(text, code)
        meta['source_file'] = os.path.basename(path)

        with open(path, 'rb') as f:
            h = hashlib.md5(f.read()).hexdigest()[:10]
        meta['master_fund_id'] = hash2master.setdefault(h, 'M' + h)

        sec = split_sections(text)                        # 파싱용: 원본 레이아웃 유지
        sec_store = {k: tidy(v) for k, v in sec.items()}  # 저장용: 정렬 공백 제거
        prof = dict(meta)
        for k in SECTION_KEYS:
            prof[k] = sec_store.get(k, '')
        profiles.append(prof)

        for k in SECTION_KEYS:
            body = sec_store.get(k, '')
            if not body:
                continue
            for i, ch in enumerate(chunks_of(body)):
                sections.append(dict(
                    record_id='%s_%s_%03d' % (code, k, i),
                    product_code=code, product_name=meta['product_name'],
                    section=k, chunk_no=i, text=ch,
                    search_text=re.sub(r'\s+', ' ', ch),
                    source_file=meta['source_file']))

        fr = parse_fees(text)
        if fr:
            stat['fee_ok'] += 1
        seen = {}
        for r in fr:
            if not r['total_fee'] or r['total_fee'] <= 0:   # 헤더 오인식 방어
                continue
            nm = r['class_name']
            seen[nm] = seen.get(nm, 0) + 1
            if seen[nm] > 1:
                nm = '%s#%d' % (nm, seen[nm])
            fees.append((code, nm, r['class_desc'], r['mgmt_fee'], r['sales_fee'],
                         r['trust_fee'], r['admin_fee'], r['total_fee'],
                         r['etc_cost'], r['total_cost']))

        pr = parse_performance(sec.get('performance', ''))
        if pr:
            stat['perf_ok'] += 1
        perfs += [(code, x['kind'], x['class_name'], x['period'], x['value']) for x in pr]

        ar = parse_aum(sec.get('subscription_history', ''))
        if ar:
            stat['aum_ok'] += 1
        aums += [(code, x['class_name'], x['end_balance_million']) for x in ar]

    with open(PROFILES_JSON, 'w', encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False)
    with open(SECTIONS_JSON, 'w', encoding='utf-8') as f:
        json.dump(sections, f, ensure_ascii=False)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    c = conn.cursor()
    for p in profiles:
        c.execute("""INSERT OR REPLACE INTO fund_products
            (product_code, product_name, asset_manager, document_date, risk_grade,
             risk_label, product_type, master_fund_id, source_file)
            VALUES (?,?,?,?,?,?,?,?,?)""",
                  (p['product_code'], p['product_name'] or p['product_code'],
                   p['asset_manager'], p['document_date'], p['risk_grade'],
                   p['risk_label'], p['product_type'], p['master_fund_id'], p['source_file']))
        c.execute("INSERT OR REPLACE INTO fund_profiles (product_code,%s) VALUES (?%s)"
                  % (','.join(SECTION_KEYS), ',?' * len(SECTION_KEYS)),
                  tuple([p['product_code']] + [p.get(k, '') for k in SECTION_KEYS]))
    c.executemany("""INSERT INTO fund_class_fees
        (product_code,class_name,class_desc,mgmt_fee,sales_fee,trust_fee,admin_fee,
         total_fee,etc_cost,total_cost) VALUES (?,?,?,?,?,?,?,?,?,?)""", fees)
    c.executemany("INSERT INTO fund_performance (product_code,kind,class_name,period,value)"
                  " VALUES (?,?,?,?,?)", perfs)
    c.executemany("INSERT INTO fund_aum (product_code,class_name,end_balance_million)"
                  " VALUES (?,?,?)", aums)
    c.executemany("""INSERT OR REPLACE INTO fund_sections
        (record_id,product_code,product_name,section,chunk_no,text,search_text,source_file)
        VALUES (?,?,?,?,?,?,?,?)""",
                  [(s['record_id'], s['product_code'], s['product_name'], s['section'],
                    s['chunk_no'], s['text'], s['search_text'], s['source_file'])
                   for s in sections])
    conn.commit()
    conn.close()

    n = len(pdfs)
    print('=' * 62)
    print('펀드 DB 재구축 완료:', os.path.basename(DB_PATH))
    print('=' * 62)
    print('  투자설명서            %6d건' % n)
    print('  fund_products         %6d행' % len(profiles))
    print('  fund_class_fees       %6d행   (성공 %d/%d 펀드)' % (len(fees), stat['fee_ok'], n))
    print('  fund_performance      %6d행   (성공 %d/%d 펀드)' % (len(perfs), stat['perf_ok'], n))
    print('  fund_aum              %6d행   (성공 %d/%d 펀드)' % (len(aums), stat['aum_ok'], n))
    print('  fund_sections         %6d행' % len(sections))
    print('  고유 투자설명서       %6d건   (중복 %d건은 master_fund_id 로 묶음)'
          % (len(hash2master), n - len(hash2master)))


if __name__ == '__main__':
    main()
