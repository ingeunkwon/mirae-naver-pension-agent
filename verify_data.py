# -*- coding: utf-8 -*-
"""재생성 결과 검증 리포트 (API 불필요).

원본 폴더와 산출물을 대조해 문서별 수록률, 표/수치 추출 성공률, 공식 예시 질의
대응 가능 여부를 한 번에 출력한다.
"""
import glob
import json
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def norm(s):
    return re.sub(r'\s+', '', s or '')


def head(t):
    print()
    print('=' * 64)
    print(t)
    print('=' * 64)


def check_kb():
    from parsers.docx_reader import read_docx
    old = os.path.join(BASE_DIR, 'knowledge_base_final.json')
    new = os.path.join(BASE_DIR, 'knowledge_base_v2.json')
    if not os.path.exists(new):
        print('knowledge_base_v2.json 이 없습니다. build_knowledge_base.py 를 먼저 실행하세요.')
        return
    kb_o = json.load(open(old, encoding='utf-8')) if os.path.exists(old) else []
    kb_n = json.load(open(new, encoding='utf-8'))
    head('1. 제도 문서 지식베이스')
    print('  레코드   %5d건 -> %5d건' % (len(kb_o), len(kb_n)))
    print('  본문총량 %5d자 -> %5d자' % (sum(len(norm(r['text'])) for r in kb_o),
                                        sum(len(norm(r['text'])) for r in kb_n)))
    print()
    print('  [docx 본문 수록률] 원본 문단 대비')
    bad = 0
    for f in sorted(glob.glob(os.path.join(BASE_DIR, 'docs_renamed', '*.docx')),
                    key=lambda x: int(re.search(r'doc(\d+)', x).group(1))):
        doc = os.path.basename(f).split('.')[0]
        blocks = read_docx(f)
        src = sum(len(norm(b['text'])) for b in blocks if b['kind'] == 'paragraph')
        got = sum(len(norm(r['text'])) for r in kb_n
                  if r['document'] == doc and r['content_type'] in ('paragraph', 'manual'))
        ratio = got / src * 100 if src else 100
        flag = '' if ratio >= 70 else '  <-- 확인 필요'
        if ratio < 70:
            bad += 1
        print('    %-7s %6d자 -> %6d자  %5.0f%%%s' % (doc, src, got, ratio, flag))
    print('  수록률 70%% 미만 문서: %d건' % bad)
    types = {}
    for r in kb_n:
        types[r['content_type']] = types.get(r['content_type'], 0) + 1
    print()
    print('  content_type:', dict(sorted(types.items(), key=lambda x: -x[1])))


def check_fund():
    db = os.path.join(BASE_DIR, 'data', 'fund_prospectus_v2.sqlite')
    if not os.path.exists(db):
        print('fund_prospectus_v2.sqlite 이 없습니다. build_fund_db.py 를 먼저 실행하세요.')
        return
    c = sqlite3.connect(db)
    head('2. 펀드 데이터베이스')
    for t in ['fund_products', 'fund_profiles', 'fund_class_fees',
              'fund_performance', 'fund_aum', 'fund_sections']:
        print('  %-18s %6d행' % (t, c.execute('select count(*) from %s' % t).fetchone()[0]))
    print()
    print('  [상품 6축 커버리지]')
    n = c.execute('select count(*) from fund_products').fetchone()[0]
    axes = [
        ('상품분류(자산유형)', 'select count(*) from fund_products where product_type is not null'),
        ('위험등급', 'select count(*) from fund_products where risk_grade is not null'),
        ('판매 클래스', 'select count(distinct product_code) from fund_class_fees'),
        ('총보수', 'select count(distinct product_code) from fund_class_fees where total_fee>0'),
        ('수익률', 'select count(distinct product_code) from fund_performance'),
        ('시장잔고', 'select count(distinct product_code) from fund_aum'),
    ]
    for name, q in axes:
        v = c.execute(q).fetchone()[0]
        print('    %-18s %3d/%d  %s' % (name, v, n, '정상' if v >= n * 0.7 else '부분'))
    print()
    print('  [tax 섹션 폭주 여부] 평균 길이')
    for k in ['fees', 'tax', 'financials', 'performance']:
        v = c.execute('select avg(length(%s)) from fund_profiles' % k).fetchone()[0] or 0
        print('    %-14s %8.0f자' % (k, v))


def check_queries():
    db = os.path.join(BASE_DIR, 'data', 'fund_prospectus_v2.sqlite')
    if not os.path.exists(db):
        return
    c = sqlite3.connect(db)
    head('3. 공식 예시 질의 대응 점검')
    print('  Q. "솔로몬 국공채 단기/중장기/장기, 뭐가 달라요? 안정적인 걸 원해요."')
    q = """select p.product_name, p.risk_grade, f.total_fee,
                  (select value from fund_performance v
                    where v.product_code=p.product_code and v.kind='fund' and v.period='1y' limit 1)
           from fund_products p join fund_class_fees f on p.product_code=f.product_code
           where p.product_name like '%솔로몬%국공채%' and f.class_name='C-P'
           order by p.product_name"""
    rows = list(c.execute(q))
    for r in rows:
        print('    %-42s 위험 %s등급  총보수 %.2f%%  1년 %s' %
              (r[0][:42], r[1], r[2], ('%.2f%%' % r[3]) if r[3] else '-'))
    print('    -> 답변 가능' if rows else '    -> 데이터 없음')


if __name__ == '__main__':
    check_kb()
    check_fund()
    check_queries()
