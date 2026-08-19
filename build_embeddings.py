# -*- coding: utf-8 -*-
"""임베딩 인덱스 재생성  ->  data/vector_db/rag_embeddings_v3.json

CLOVA Studio Embedding v2 API 를 호출하므로 이 스크립트만 인터넷과 API 키가 필요하다.

비용 절감: 기존 rag_embeddings_v2.json 에 같은 본문이 이미 있으면 그 벡터를 재사용한다.
따라서 실제 API 호출은 '새로 복구된 레코드' 수만큼만 발생한다.

사용법
    python build_embeddings.py             # 제도 문서만 (권장, 기본)
    python build_embeddings.py --funds     # 펀드 투자설명서 핵심 섹션까지 포함
"""
import json
import os
import re
import sqlite3
import sys
import time
import uuid

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))
API_KEY = os.getenv('CLOVASTUDIO_API_KEY')
if not API_KEY:
    raise RuntimeError('.env 에서 CLOVASTUDIO_API_KEY 를 찾을 수 없습니다.')

EMBEDDING_URL = 'https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2'
TIMEOUT = 60

IN_KB = os.path.join(BASE_DIR, 'knowledge_base_v2.json')
OLD_EMB = os.path.join(BASE_DIR, 'data', 'vector_db', 'rag_embeddings_v2.json')
OUT_EMB = os.path.join(BASE_DIR, 'data', 'vector_db', 'rag_embeddings_v3.json')
FUND_DB = os.path.join(BASE_DIR, 'data', 'fund_prospectus_v2.sqlite')
VEC_CACHE = os.path.join(BASE_DIR, '.cache_embed.json')   # 중단되어도 이어서 하도록
FUND_SECTIONS = ('investment_objective', 'investment_strategy', 'investment_risk')

# 규칙 기반 메타데이터 태깅.
# 기존 인덱스가 쓰던 태그 어휘를 따르되, 검색 코드와 데이터가 어긋나 있던 표기를 정리한다.
PENSION_RULES = [
    ('IRP', ['irp', '개인형퇴직연금', '개인형 퇴직연금']),
    ('DC', ['dc형', '확정기여', 'dc제도', 'dc ']),
    ('DB', ['db형', '확정급여', 'db제도', 'db ']),
    ('연금저축', ['연금저축']),
    ('퇴직연금_공통', ['퇴직연금']),
    ('과학기술인연금', ['과학기술인', '과기공']),
    ('ISA_연계', ['isa']),
    ('디폴트옵션', ['디폴트옵션', '사전지정운용']),
]
TOPIC_RULES = [
    ('중도인출·해지', ['중도인출', '중간인출', '해지']),
    ('세금·세액공제·재원확정', ['세액공제', '연금소득세', '퇴직소득세', '종합과세', '과세이연',
                               '절세', '원천징수', '세율', '비과세']),
    ('부담금·납입', ['부담금', '납입한도', '추가납입', '납입', '적립']),
    ('운용·매매', ['위험자산', '투자한도', '매수', '매도', '리밸런싱', '운용지시', '운용방법']),
    ('이전·전환·승계', ['이전', '이관', '계좌이체', '전환', '승계']),
    ('연금개시·수령', ['연금개시', '연금수령', '수령연차', '연금받', '연금지급', '수급']),
    ('권리보호·담보대출·압류', ['압류', '담보대출', '수급권', '담보융자']),
    ('디폴트옵션', ['디폴트옵션', '사전지정운용']),
    ('제도·가입', ['가입자격', '가입대상', '가입조건', '가입할 수', '가입 가능', '가입 불가',
                  '퇴직연금이 무엇', '확정급여형', '확정기여형', '퇴직급여제도', '제도를 설정',
                  '가입자란', '적용범위']),
    ('급여·산정', ['평균임금', '급여산정', '퇴직급여 계산', '급여 지급', '지급 종류', '산정방법']),
    ('규약·교육', ['규약', '교육']),
    ('시스템·업무절차', ['신청', '등록', '협약', '업무처리', '장내상품', '조회', '서류', '절차']),
]
ASPECT_RULES = [
    ('중도인출', ['중도인출']), ('해지', ['해지']), ('세액공제', ['세액공제']),
    ('납입한도', ['납입한도']), ('위험자산', ['위험자산']), ('투자한도', ['투자한도']),
    ('계약이전', ['이전', '이관']), ('연금수령', ['연금수령']), ('연금개시', ['연금개시']),
    ('압류', ['압류']), ('담보대출', ['담보대출']), ('디폴트옵션', ['디폴트옵션']),
    ('종합과세', ['종합과세']), ('과세이연', ['과세이연']), ('퇴직소득세', ['퇴직소득세']),
]


def tag(text):
    t = re.sub(r'\s+', ' ', (text or '')).lower()

    def hit(rules):
        return [k for k, kws in rules if any(w in t for w in kws)]

    topics = hit(TOPIC_RULES)
    return {
        'pension_types': hit(PENSION_RULES),
        'primary_topic': topics[0] if topics else None,
        'secondary_topics': topics[1:],
        'knowledge_aspects': hit(ASPECT_RULES),
    }


def norm(s):
    return re.sub(r'\s+', '', s or '')


def headers():
    return {'Authorization': 'Bearer %s' % API_KEY,
            'X-NCP-CLOVASTUDIO-REQUEST-ID': str(uuid.uuid4()),
            'Content-Type': 'application/json'}


LAST_ERROR = {}


def embed(text, retry=5):
    """실패해도 예외를 던지지 않고 None 을 돌려준다.

    한 청크가 실패했다고 전체 작업이 죽으면 그때까지의 API 호출이 모두 날아간다.
    429(호출 한도)가 가장 흔하므로 지수 백오프로 충분히 기다렸다가 재시도한다.
    """
    for i in range(retry):
        try:
            r = requests.post(EMBEDDING_URL, headers=headers(),
                              json={'text': text[:6000]}, timeout=TIMEOUT)
            if r.status_code == 200:
                v = r.json().get('result', {}).get('embedding')
                if v:
                    return v
                LAST_ERROR['msg'] = 'HTTP 200 이지만 embedding 이 비어 있음: %s' % r.text[:200]
            else:
                LAST_ERROR['msg'] = 'HTTP %d: %s' % (r.status_code, r.text[:200])
                if r.status_code == 429:
                    wait = float(r.headers.get('Retry-After', 0)) or (2 ** (i + 1))
                    time.sleep(min(wait, 60))
                    continue
        except requests.RequestException as e:
            LAST_ERROR['msg'] = '네트워크 오류: %s' % e
        time.sleep(2 ** (i + 1))
    return None


def build_embedding_text(rec, meta):
    head = []
    if meta['primary_topic']:
        head.append('[Primary Topic: %s]' % meta['primary_topic'])
    if meta['pension_types']:
        head.append('[Pension Types: %s]' % ', '.join(meta['pension_types']))
    if meta['knowledge_aspects']:
        head.append('[Knowledge Aspects: %s]' % ', '.join(meta['knowledge_aspects']))
    title = ' > '.join(x for x in [rec.get('major_title'), rec.get('sub_title')] if x)
    if title:
        head.append('[문서구조: %s]' % title)
    head.append('[자료유형: %s]' % rec.get('content_type'))
    return '\n'.join(head + [rec.get('text') or ''])


def main():
    include_funds = '--funds' in sys.argv
    with open(IN_KB, encoding='utf-8') as f:
        kb = json.load(f)

    cache = {}
    if os.path.exists(OLD_EMB):
        with open(OLD_EMB, encoding='utf-8') as f:
            for r in json.load(f):
                cache[norm(r.get('text'))] = r.get('embedding')
    print('기존 벡터 캐시 %d건 로드' % len(cache))
    fresh = {}
    if os.path.exists(VEC_CACHE):          # 이전 실행에서 새로 만든 벡터
        with open(VEC_CACHE, encoding='utf-8') as f:
            fresh = json.load(f)
        cache.update(fresh)
        print('이전 실행분 벡터 %d건 이어받음' % len(fresh))

    def save_cache():
        with open(VEC_CACHE, 'w', encoding='utf-8') as f:
            json.dump(fresh, f)

    items = [dict(kind='doc', rec=r, text=r.get('text') or '',
                  source_file=r.get('file_name'),
                  page_start=r.get('start_page'), page_end=r.get('end_page')) for r in kb]

    if include_funds and os.path.exists(FUND_DB):
        conn = sqlite3.connect(FUND_DB)
        q = ('select record_id, product_code, product_name, section, text, source_file '
             'from fund_sections where section in (%s)' % ','.join('?' * len(FUND_SECTIONS)))
        for rid, code, name, sec, txt, src in conn.execute(q, FUND_SECTIONS):
            items.append(dict(kind='fund', rec={
                'record_id': rid, 'document': code, 'file_name': src,
                'content_type': 'fund_section', 'major_title': name,
                'sub_title': sec, 'text': txt}, text=txt,
                source_file=src, page_start=None, page_end=None))
        conn.close()
        print('펀드 섹션 %d건 포함' % sum(1 for x in items if x['kind'] == 'fund'))

    out, reused, called, failed = [], 0, 0, []
    for i, it in enumerate(items, 1):
        rec, txt = it['rec'], it['text']
        if len(norm(txt)) < 15:
            continue
        meta = tag(txt + ' ' + (rec.get('major_title') or ''))
        etext = build_embedding_text(rec, meta)
        key = norm(txt)
        vec = cache.get(key)
        if vec:
            reused += 1
        else:
            time.sleep(0.25)            # 호출 한도(429) 예방용 최소 간격
            vec = embed(etext)
            called += 1
            if vec is None:                 # 실패한 청크는 건너뛰고 계속 진행
                failed.append((rec.get('record_id'), LAST_ERROR.get('msg', '')))
                print('  [건너뜀] %s | %s' % (rec.get('record_id'), LAST_ERROR.get('msg', '')[:120]))
                continue
            fresh[key] = vec
            if called % 20 == 0:
                save_cache()
        out.append({
            'chunk_id': 'rag_%06d' % len(out),
            'source_record_id': rec.get('record_id'),
            'source_file': it['source_file'],
            'document': rec.get('document'),
            'content_type': rec.get('content_type'),
            'primary_topic': meta['primary_topic'],
            'secondary_topics': meta['secondary_topics'],
            'pension_types': meta['pension_types'],
            'knowledge_aspects': meta['knowledge_aspects'],
            'major_title': rec.get('major_title'),
            'sub_title': rec.get('sub_title'),
            'page_start': it['page_start'], 'page_end': it['page_end'],
            'text': txt, 'embedding_text': etext,
            'embedding': vec, 'embedding_dimension': len(vec),
            'embedding_model': 'CLOVA Studio Embedding v2',
        })
        if i % 50 == 0:
            print('  %d/%d  (재사용 %d / API %d)' % (i, len(items), reused, called))

    save_cache()
    os.makedirs(os.path.dirname(OUT_EMB), exist_ok=True)
    with open(OUT_EMB, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('=' * 62)
    print('임베딩 인덱스 생성 완료:', os.path.basename(OUT_EMB))
    print('  청크 %d건  (기존 벡터 재사용 %d / 신규 API 호출 %d)' % (len(out), reused, called))
    if failed:
        print('  실패로 건너뛴 청크 %d건:' % len(failed))
        for rid, msg in failed[:10]:
            print('    - %s | %s' % (rid, msg[:110]))
        print('  다시 실행하면 성공한 벡터는 재사용하고 실패분만 재시도합니다.')


if __name__ == '__main__':
    main()
