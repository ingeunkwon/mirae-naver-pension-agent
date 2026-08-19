# -*- coding: utf-8 -*-
"""제도 문서 지식베이스 재구축  ->  knowledge_base_v2.json

기존 knowledge_base_final.json 을 보존하고 누락분만 더하는 증분 복구 방식이다.
스캔 PDF 는 CLOVA OCR 로 만들어진 레코드라 오프라인에서 재생성할 수 없기 때문에,
검증된 기존 레코드를 버리지 않는 것이 안전하다.

복구 대상
  1) docx 본문 누락 : 표 전용 파서만 돌아 문단이 통째로 빠진 문서
                      (doc23, 25, 36, 38, 40, 41, 42, 43, 51, 52 등)
  2) 법령 첨부 누락 : 안내문 PDF 말미의 관련법령/행정해석 조문
"""
import glob
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from parsers.docx_reader import read_docx
from parsers.legal_reader import extract_legal
from parsers.pdf_text import extract_text

SRC_DIR = os.path.join(BASE_DIR, 'docs_renamed')
IN_KB = os.path.join(BASE_DIR, 'knowledge_base_final.json')
OUT_KB = os.path.join(BASE_DIR, 'knowledge_base_v2.json')

MAX_CHUNK = 1100          # 임베딩 청크가 지나치게 길어지지 않도록
MIN_KEEP = 25             # 이보다 짧은 조각은 단독 레코드로 만들지 않는다


def norm(s):
    return re.sub(r'\s+', '', s or '')


def is_heading(text):
    t = text.strip()
    return len(t) <= 45 and not t.endswith(('.', '다', '요', ':', '니다'))


def group_paragraphs(blocks):
    """제목처럼 보이는 문단을 기준으로 본문을 묶어 검색 단위 청크를 만든다."""
    chunks, cur, title = [], [], None

    def flush():
        if cur:
            chunks.append({'title': title, 'text': '\n'.join(cur)})

    for b in blocks:
        if b['kind'] != 'paragraph':
            continue
        t = b['text']
        if is_heading(t):
            flush()
            cur, title = [], t
            continue
        if cur and len(norm('\n'.join(cur)) + norm(t)) > MAX_CHUNK:
            flush()
            cur = []
        cur.append(t)
    flush()
    return [c for c in chunks if len(norm(c['text'])) >= MIN_KEEP]


def main():
    with open(IN_KB, encoding='utf-8') as f:
        kb = json.load(f)
    existing = {}
    for r in kb:
        existing.setdefault(r.get('document'), []).append(norm(r.get('text')))
    for k in existing:
        existing[k] = ''.join(existing[k])

    added = []
    stats = {'body': 0, 'legal': 0, 'docs_body': set(), 'docs_legal': set()}
    seq = 0

    for path in sorted(glob.glob(os.path.join(SRC_DIR, '*.docx')),
                       key=lambda x: int(re.search(r'doc(\d+)', x).group(1))):
        doc = os.path.basename(path).split('.')[0]
        have = existing.get(doc, '')
        for c in group_paragraphs(read_docx(path)):
            body = norm(c['text'])
            if body and body in have:      # 이미 지식베이스에 있는 내용은 건너뛴다
                continue
            seq += 1
            added.append({
                'document': doc, 'file_name': doc + '.docx', 'file_type': 'docx',
                'section_number': None, 'major_heading': None, 'major_number': None,
                'major_title': c['title'], 'sub_heading': None, 'sub_label': None,
                'sub_title': None, 'start_page': None, 'end_page': None,
                'text': (c['title'] + '\n' + c['text']) if c['title'] else c['text'],
                'content_type': 'paragraph', 'source_parser': 'docx_body_v2',
                'record_id': 'v2_body_%05d' % seq,
                'search_text': (c['title'] or '') + ' ' + c['text'],
                'quality': 'ok', 'needs_review': False, 'review_reasons': [],
            })
            stats['body'] += 1
            stats['docs_body'].add(doc)

    for path in sorted(glob.glob(os.path.join(SRC_DIR, '*.pdf')),
                       key=lambda x: int(re.search(r'doc(\d+)', x).group(1))):
        doc = os.path.basename(path).split('.')[0]
        have = existing.get(doc, '')
        for a in extract_legal(extract_text(path)):
            if norm(a['text'])[:120] in have:
                continue
            seq += 1
            added.append({
                'document': doc, 'file_name': doc + '.pdf', 'file_type': 'pdf',
                'section_number': None, 'major_heading': None, 'major_number': None,
                'major_title': '관련법령 행정해석 판례', 'sub_heading': None,
                'sub_label': None, 'sub_title': a['law_title'],
                'start_page': None, 'end_page': None,
                'text': a['text'], 'content_type': 'legal_reference',
                'source_parser': 'legal_reader_v2',
                'record_id': 'v2_law_%05d' % seq,
                'search_text': a['law_title'] + ' ' + a['text'],
                'quality': 'ok', 'needs_review': False, 'review_reasons': [],
            })
            stats['legal'] += 1
            stats['docs_legal'].add(doc)

    out = kb + added
    with open(OUT_KB, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print('=' * 62)
    print('지식베이스 재구축 완료:', os.path.basename(OUT_KB))
    print('=' * 62)
    print('  기존 레코드           %6d건' % len(kb))
    print('  + docx 본문 복구      %6d건  (%d개 문서)' % (stats['body'], len(stats['docs_body'])))
    print('  + 관련법령 조문 추가  %6d건  (%d개 문서)' % (stats['legal'], len(stats['docs_legal'])))
    print('  최종 레코드           %6d건' % len(out))
    print('  본문 총량             %6d자  (기존 %d자)' % (
        sum(len(norm(r.get('text'))) for r in out),
        sum(len(norm(r.get('text'))) for r in kb)))


if __name__ == '__main__':
    main()
