# -*- coding: utf-8 -*-
"""PDF 텍스트 추출. 레이아웃 보존이 중요하므로(표 파싱) 다음 순서로 시도한다.

1) poppler 의 pdftotext -layout  : 가장 빠르고 정확. 리눅스/맥에서 보통 설치돼 있다.
2) pdfplumber extract_text(layout=True) : 순수 파이썬. 윈도우 기본 경로.

추출 결과는 .cache_pdftext/ 에 캐시하므로 재실행이 빠르다.
"""
import hashlib
import os
import shutil
import subprocess

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.cache_pdftext')
_HAS_PDFTOTEXT = shutil.which('pdftotext') is not None


def _cache_path(path):
    key = hashlib.md5((os.path.abspath(path) + str(os.path.getmtime(path))).encode()).hexdigest()
    return os.path.join(CACHE_DIR, key + '.txt')


def extract_text(path, use_cache=True):
    cp = _cache_path(path)
    if use_cache and os.path.exists(cp):
        with open(cp, encoding='utf-8') as f:
            return f.read()
    if _HAS_PDFTOTEXT:
        txt = subprocess.run(['pdftotext', '-layout', path, '-'],
                             capture_output=True).stdout.decode('utf-8', 'ignore')
    else:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            txt = '\n'.join((pg.extract_text(layout=True) or '') for pg in pdf.pages)
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cp, 'w', encoding='utf-8') as f:
            f.write(txt)
    return txt


def tidy(txt):
    """레이아웃 정렬용 공백을 줄인다.

    pdfplumber 의 layout 모드는 열을 맞추려고 공백을 대량으로 넣는다(pdftotext 대비 2.4배).
    그대로 저장하면 청크가 공백으로 채워져 검색 품질과 DB 크기가 모두 나빠진다.
    표의 줄 구조는 살려야 하므로 줄바꿈은 유지하고 연속 공백만 2칸으로 줄인다.

    주의: 보수표/운용실적 파서는 원본 레이아웃을 기준으로 검증했으므로
    파싱에는 원본을, 저장(fund_profiles / fund_sections)에는 이 함수 결과를 쓴다.
    """
    import re as _re
    out = []
    for ln in txt.split('\n'):
        ln = _re.sub(r'[ \t]{2,}', '  ', ln.rstrip())
        if ln.strip():
            out.append(ln)
    return '\n'.join(out)
