import json
import re
import sqlite3
from pathlib import Path
import requests
from rag_agent import build_headers, post_with_retry, TIMEOUT, HCX_URL

BASE_DIR = Path(__file__).resolve().parent

FIN_SCHEMA = """
[financial_data.sqlite - 연금 제도/세제 정형 데이터]

1. pension_tax_limits  -- 소득 구간별 연금계좌 세액공제. 금액 단위는 원.
   income_tier          소득 구간 (총급여 5,500만원 이하 / 초과)
   deduction_rate       공제율. 0.165 = 16.5%, 0.132 = 13.2%
   pension_limit        ★세액공제 대상 납입한도 (연금저축+IRP 합산). 9,000,000
   isa_additional_limit ISA 만기자금 전환 시 추가 한도. 3,000,000
   total_max_deposit    pension_limit + isa_additional_limit 합계. 12,000,000
   total_max_benefit    최대 환급 세액 = pension_limit x deduction_rate

   ※ 컬럼 선택 주의
     "세액공제 얼마까지 되나요" 는 pension_limit 이 답이다. total_max_deposit
     (ISA 전환분까지 더한 총 납입한도)를 쓰면 틀린 답이 된다.
     세액공제 질문에는 income_tier, deduction_rate, pension_limit,
     total_max_benefit 을 함께 조회해 소득 구간별로 비교할 수 있게 한다.

2. pension_age_tax_rates  -- 연령대별 연금소득세율
   age_category, min_age, max_age, tax_rate, tax_type
   55~69세 5.5% / 70~79세 4.4% / 80세 이상 3.3%
   특정 나이를 물으면 min_age <= 나이 AND 나이 <= max_age 로 조회한다.

3. pension_seizure_rules   -- 자산 유형별 압류 가능 여부
   asset_type, is_seizable, seizure_ratio, legal_basis

4. pension_withdrawal_rules -- 중도인출 사유별 허용 여부와 과세율
   reason, is_statutory_allowed, is_tax_exempt_reason, tax_rate_label, tax_rate

5. document_tables  -- 안내문에서 추출한 표 원문
   record_id, document, file_name, page, table_title, table_text
   위 4개 표로 답할 수 없을 때 table_text LIKE 검색에 사용한다.
"""

FUND_SCHEMA = """
[fund_prospectus_v2.sqlite - 펀드 투자설명서 정형 데이터 (100종)]

1. fund_products (product_code TEXT PK, product_name TEXT, asset_manager TEXT,
     document_date TEXT, risk_grade INTEGER, risk_label TEXT, product_type TEXT,
     master_fund_id TEXT, source_file TEXT)
   -- risk_grade 는 국내 표준 표기다. 숫자가 작을수록 위험하다.
      1=매우 높은 위험, 2=높은 위험, 3=다소 높은 위험,
      4=보통 위험, 5=낮은 위험, 6=매우 낮은 위험
      따라서 "안정적인/보수적인 상품"은 risk_grade >= 5,
             "공격적인/고위험 상품"은 risk_grade <= 2 로 조회한다.
   -- product_type: 채권형 / 주식형 / 주식-파생형 / 재간접형 / MMF 등
   -- master_fund_id: 같은 투자설명서를 공유하는 클래스 묶음. 상품 비교 시
      GROUP BY master_fund_id 로 중복 나열을 피할 수 있다.

2. fund_class_fees (product_code TEXT, class_name TEXT, class_desc TEXT,
     mgmt_fee REAL, sales_fee REAL, trust_fee REAL, admin_fee REAL,
     total_fee REAL, etc_cost REAL, total_cost REAL)
   -- 판매 클래스별 보수. 단위는 연 %. total_fee 가 총보수(TER).

   ★ 클래스 코드 해석 규칙 (class_desc 는 PDF 줄바꿈으로 문자열이 깨져 있으므로
     절대 인용하지 말고, 아래 규칙으로 설명한다)
       P  계열  (C-P, C-PE, S-P, C-P1 ...)        -> 연금저축(개인연금)
       P2 계열  (C-P2, C-P2E, S-P2, C-P2I ...)    -> 퇴직연금
       R  계열  (C-R, C-RJ, C-RF, C-RI ...)       -> 퇴직연금
       코드 끝에 E 가 붙으면 온라인, 없으면 오프라인. S 로 시작하면 온라인슈퍼.
       A = 수수료선취, C = 수수료미징구.
     예) C-P  = 수수료미징구-오프라인-개인연금(연금저축)
         C-PE = 수수료미징구-온라인-개인연금(연금저축)
         C-P2 = 수수료미징구-오프라인-퇴직연금
         C-P2E= 수수료미징구-온라인-퇴직연금

   ★ 클래스 필터는 반드시 LIKE 로 쓴다. 정확히 일치(=)로 좁히면 변형 클래스를
     통째로 놓친다. 실제로 'C-P2' 로만 좁혀 최저 보수 펀드를 놓친 사고가 있었다.
       (X) WHERE class_name = 'C-P2'
       (O) 퇴직연금:  WHERE (class_name LIKE '%P2%' OR class_name LIKE '%-R%')
       (O) 연금저축:  WHERE (class_name LIKE '%-P%' AND class_name NOT LIKE '%P2%')
       (O) 연금계좌 전체: WHERE (class_name LIKE '%-P%' OR class_name LIKE '%-R%')

3. fund_performance (product_code TEXT, kind TEXT, class_name TEXT, period TEXT, value REAL)
   -- 연평균 수익률(%). kind: fund(펀드 전체) / class(클래스별) / benchmark(비교지수)
      / volatility(수익률 변동성).  period: 1y, 2y, 3y, 5y, since(설정이후)

4. fund_aum (product_code TEXT, class_name TEXT, end_balance_million INTEGER)
   -- 최근 회계기간말 잔고. 단위는 백만원(원문이 억원/천원이어도 백만원으로 환산 저장).
      시장잔고(설정규모) 판단에 사용한다.
      class_name 이 '_FUND' 이면 클래스 구분 없는 펀드 전체 합계다.
      투자설명서에 설정.환매현황이 없는 신규 펀드는 이 표에 행이 없다(약 절반).
      따라서 잔고를 물으면 LEFT JOIN 으로 조회하고, 값이 없으면 "설정 규모는
      제공된 투자설명서에 기재되어 있지 않습니다" 라고 한계를 밝힌다.

5. fund_profiles (product_code TEXT PK, investment_objective, investment_target,
     investment_strategy, investment_risk, purchase_redemption, valuation,
     fees, tax, financials, subscription_history, performance)
   -- 투자설명서 섹션 원문. 정성 질의는 LIKE 검색.

6. fund_sections (record_id TEXT PK, product_code TEXT, product_name TEXT,
     section TEXT, chunk_no INTEGER, text TEXT, search_text TEXT)
   -- 위 섹션을 청크로 나눈 것. 특정 문구를 찾을 때 search_text LIKE 로 검색.
"""


def _extract_sql(content):
    """모델 응답에서 SELECT 문을 최대한 살려서 뽑아낸다.

    모델이 JSON 뒤에 설명을 덧붙이면 json.loads 가 'Extra data' 로 실패하고,
    그러면 DB 조회 결과가 통째로 비어 LLM 이 일반 상식으로 답을 메운다.
    실제로 공식 예시 질의(솔로몬 국공채 비교)가 이 경로로 무너졌다.
    """
    if not content:
        return None
    c = content.strip()
    if c.startswith("```"):
        c = re.sub(r'^```[a-zA-Z]*\s*', '', c)
        c = re.sub(r'```\s*$', '', c).strip()

    # 1) 정상 JSON
    try:
        v = json.loads(c)
        if isinstance(v, dict) and v.get("sql"):
            return v["sql"]
    except Exception:
        pass
    # 2) 앞쪽 JSON 객체만 잘라서 재시도 (뒤에 설명이 붙은 경우)
    depth, start = 0, None
    for i, ch in enumerate(c):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    v = json.loads(c[start:i + 1])
                    if isinstance(v, dict) and v.get("sql"):
                        return v["sql"]
                except Exception:
                    pass
                break
    # 3) sql 값만 정규식으로
    m = re.search(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', c, re.S)
    if m:
        return m.group(1).replace('\\"', '"').replace('\\n', ' ').replace('\\\\', '\\')
    # 4) 맨몸 SELECT 문
    m = re.search(r'(SELECT\b[\s\S]*?)(?:;|\n\s*\n|$)', c, re.I)
    if m:
        return m.group(1).strip()
    return None


def _clean_sql(sql):
    """주석/설명이 섞여 들어온 SQL 을 정리한다."""
    if not sql:
        return None
    sql = re.sub(r'--[^\n]*', ' ', sql)          # 라인 주석
    sql = re.sub(r'/\*[\s\S]*?\*/', ' ', sql)   # 블록 주석
    sql = re.sub(r'\s+', ' ', sql).strip().rstrip(';').strip()
    m = re.search(r'\bSELECT\b[\s\S]*', sql, re.I)
    return m.group(0).strip() if m else None


# ──────────────────────────────────────────────────────────────────────
# 실행 후 안전장치
#
# LLM 이 만든 SQL 의 ORDER BY 를 믿지 않는다. "가장 싼 게 뭔가요?" 질의에서
# ORDER BY 가 빠지거나 클래스 필터가 좁아 최솟값을 놓친 사례가 실제로 있었다
# (평가 Q-014: 0.155% 상품을 두고 0.26% 를 "가장 낮다"고 답함).
# 숫자 컬럼이 있고 질의에 최저/최고 신호가 있으면 파이썬에서 다시 정렬한다.
# ──────────────────────────────────────────────────────────────────────

SORT_ASC_WORDS = ['가장 싼', '가장 낮은', '가장 저렴', '가장 적은', '제일 싼',
                  '제일 낮은', '제일 저렴', '최저', '싼 순', '낮은 순', '저렴한 순']
SORT_DESC_WORDS = ['가장 비싼', '가장 높은', '제일 비싼', '제일 높은',
                   '최고', '비싼 순', '높은 순']
FEE_COLS = ('total_fee', 'fee_total', 'total_cost')

CLASS_RULES = [
    ('P2', '퇴직연금'), ('-R', '퇴직연금'), ('-P', '연금저축(개인연금)'),
]

# ──────────────────────────────────────────────────────────────────────
# class_name 좁은 필터 안전장치
#
# 프롬프트에 "LIKE 로 넓게 써라"를 명시해도 LLM 이 여전히
# WHERE class_name = 'C-P2' 식으로 정확히 일치시켜 C-P2E·S-P2·C-RJ 같은
# 변형 클래스를 통째로 놓치는 사고가 실제로 재현됐다(평가 Q-014/Q-017).
# ORDER BY 안전장치와 같은 이유로, LLM 이 만든 WHERE 절도 신뢰하지 않고
# 코드에서 강제로 넓힌다.
# ──────────────────────────────────────────────────────────────────────

#   테이블 별칭이 붙은 경우(예: "fc.class_name = 'C-P'")까지 정규식이
#   함께 소비해야 한다. 별칭 접두사를 남겨두고 값만 바꾸면
#   "fc.(class_name LIKE ...)" 같은 깨진 SQL이 되어 unrecognized token
#   에러가 난다(평가 Q-017에서 실제로 재현됨). class_name 앞에 붙는
#   "별칭." 도 매치에 포함시켜 통째로 바꿔치기한다.
_CLASS_EQ_RE = re.compile(r"(?:\w+\.)?class_name\s*=\s*'([A-Za-z0-9\-]+)'", re.I)
_CLASS_IN_RE = re.compile(r"(?:\w+\.)?class_name\s+IN\s*\(\s*'([A-Za-z0-9\-]+)'[^)]*\)", re.I)
# 질문에 클래스 코드가 직접 등장하면(예: "C-P2E 클래스 보수가 얼마?") LLM의
# 좁은 필터가 의도된 것일 수 있으므로 넓히지 않는다. 넓히는 건 "가장 싼/이하"
# 처럼 계좌유형 전체에서 비교·필터링하는 질문일 때만이다.
_CLASS_CODE_IN_QUERY_RE = re.compile(r"[CSA]-(?:P2?|R)[A-Z0-9]*", re.I)


def _broad_class_clause(code):
    """좁은 class_name 값 하나를 계좌유형 전체를 포괄하는 LIKE 절로 바꾼다."""
    c = (code or "").upper()
    if 'P2' in c or c.startswith('C-R') or c.startswith('R') or '-R' in c:
        return "(class_name LIKE '%P2%' OR class_name LIKE '%-R%')"
    if '-P' in c or c.endswith('P'):
        return "(class_name LIKE '%-P%' AND class_name NOT LIKE '%P2%')"
    return None


def _widen_class_filter(sql, query="", force=False):
    """LLM 이 class_name 을 '='나 IN 으로 좁혀 썼으면 계좌유형 전체를
    포괄하는 LIKE 절로 바꿔치기한다. (Q-014: 0.155%인 NH-Amundi를 두고
    'C-P2'로만 좁혀 0.26%짜리를 '가장 낮다'고 답한 사고)
    질문 자체가 특정 클래스 코드를 콕 집어 물었으면 기본적으로 건드리지
    않지만, force=True 면 그마저도 무시하고 넓힌다 — 좁은 매칭이 0건일 때
    재시도용으로 쓴다(Q-039: 'C-P'/'C-P2' 정확matching이 실제 저장값과
    안 맞아 헛스윙한 경우)."""
    if not sql or 'class_name' not in sql.lower():
        return sql
    if not force and _CLASS_CODE_IN_QUERY_RE.search(query or ""):
        return sql

    def _repl(m):
        broad = _broad_class_clause(m.group(1))
        return broad if broad else m.group(0)

    sql = _CLASS_EQ_RE.sub(_repl, sql)
    sql = _CLASS_IN_RE.sub(_repl, sql)
    return sql


def describe_class(code):
    """class_desc 가 깨져 있으므로 class_name 코드에서 의미를 유도한다."""
    if not code:
        return None
    c = str(code).upper().replace(' ', '')
    account = None
    if 'P2' in c:
        account = '퇴직연금'
    elif c.startswith('C-R') or c.startswith('R') or '-R' in c:
        account = '퇴직연금'
    elif '-P' in c or c.endswith('P'):
        account = '연금저축(개인연금)'
    if account is None:
        return None
    if c.startswith('S'):
        channel = '온라인슈퍼'
    elif c.endswith('E'):
        channel = '온라인'
    else:
        channel = '오프라인'
    fee = '수수료선취' if c.startswith('A') else '수수료미징구'
    return f"{fee}-{channel}-{account}"


def _postprocess(query, cols, rows):
    """최저/최고 질의는 재정렬하고, class_name 이 있으면 의미 컬럼을 덧붙인다."""
    if not rows or not cols:
        return cols, rows
    rows = [list(r) for r in rows]

    fee_idx = next((cols.index(c) for c in FEE_COLS if c in cols), None)
    if fee_idx is not None:
        q = query.lower()
        if any(w in q for w in SORT_ASC_WORDS):
            rows.sort(key=lambda r: (r[fee_idx] is None, r[fee_idx]))
        elif any(w in q for w in SORT_DESC_WORDS):
            rows.sort(key=lambda r: (r[fee_idx] is None,
                                     -(r[fee_idx] if r[fee_idx] is not None else 0)))

    if 'class_name' in cols and 'class_meaning' not in cols:
        ci = cols.index('class_name')
        cols = list(cols) + ['class_meaning']
        for r in rows:
            r.append(describe_class(r[ci]))
    return cols, rows


class PensionSQLAgent:
    def __init__(
        self,
        fin_db_path=BASE_DIR / "data" / "financial_data.sqlite",
        fund_db_path=BASE_DIR / "data" / "fund_prospectus_v2.sqlite"
    ):
        self.fin_db_path = str(fin_db_path)
        self.fund_db_path = str(fund_db_path)

    def generate_and_execute(self, query: str, db_type: str) -> dict:
        db_path = self.fin_db_path if db_type == "FIN" else self.fund_db_path
        schema_info = FIN_SCHEMA if db_type == "FIN" else FUND_SCHEMA

        prompt = f"""너는 SQLite 쿼리 생성 전문가다. 아래 스키마를 바탕으로 사용자의 질문에 정확히 부합하는 단일 SELECT SQL문만 작성하라.

{schema_info}

[작성 규칙]
- SELECT 문 하나만 작성한다. 수정/삭제 구문은 절대 쓰지 않는다.
- 상품명 검색은 반드시 키워드를 쪼개 AND LIKE 로 연결한다. 정식 상품명에는
  운용사명과 수식어가 끼어 있어 연속 문자열로는 매칭되지 않는다.
    (X) WHERE product_name LIKE '%솔로몬 국공채%'
    (O) WHERE product_name LIKE '%솔로몬%' AND product_name LIKE '%국공채%'
- 상품 비교/추천 질의는 fund_products 만 조회하지 말고 반드시
  fund_class_fees(총보수)를 조인하고, 가능하면 fund_performance(수익률)도 함께 가져온다.
  총보수 없이 상품을 비교하면 답변이 성립하지 않는다.
- 목록을 요구하는 질의는 LIMIT 을 30 이상으로 둔다. 조건에 맞는 상품을 빠뜨리면
  오답이 된다. 최소/최대 하나만 묻는 질의는 LIMIT 5 로 충분하다.
- 최저/최고/가장 싼/가장 비싼 을 묻는 질의는 반드시 ORDER BY 를 넣는다.
- 상품 비교 질의는 사람이 읽을 수 있도록 product_name 을 반드시 포함한다.
- SELECT 에 class_name 과 total_fee 를 기본으로 포함한다.

[질문]: {query}

반드시 순수 JSON 포맷으로만 출력하라:
{{"sql": "SELECT ...;"}}"""

        payload = {
            "messages": [
                {"role": "system", "content": "너는 정확한 SQL 쿼리 생성기다. JSON만 출력한다."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.0
        }

        try:
            res = post_with_retry(HCX_URL, payload).json()
            content = res.get("result", {}).get("message", {}).get("content", "").strip()
            if content.startswith("```"):
                content = content.replace("```json", "").replace("```", "").strip()
            raw_sql = _clean_sql(_extract_sql(content))
            if not raw_sql or not raw_sql.lower().startswith("select"):
                return {"error": "SELECT 문을 추출하지 못함", "sql": raw_sql,
                        "raw": content[:300], "columns": [], "data": []}

            sql = _widen_class_filter(raw_sql, query)
            cols, rows, sql_used, err = self._run(db_path, sql)
            if err and sql != raw_sql:
                # 넓히면서 SQL이 깨졌을 가능성에 대비해 원본으로 안전하게
                # 재시도한다 — 넓힌 SQL 자체의 버그가 조회를 통째로
                # 막아버리는 것보다는, 좁은 결과라도 내는 편이 낫다.
                cols, rows, sql_used, err = self._run(db_path, raw_sql)
            if err:
                return {"error": err, "sql": sql_used, "columns": [], "data": []}

            if not rows and sql_used == raw_sql and 'class_name' in raw_sql.lower():
                # 질문에 클래스 코드가 있어 넓히지 않았는데 0건이면, 코드가
                # 실제 저장 형식과 안 맞아 헛스윙했을 수 있다. 강제로 넓혀
                # 한 번 더 시도한다(Q-039).
                widened = _widen_class_filter(raw_sql, query, force=True)
                if widened != raw_sql:
                    cols2, rows2, sql2, err2 = self._run(db_path, widened)
                    if not err2 and rows2:
                        cols, rows, sql_used = cols2, rows2, sql2

            cols, rows = _postprocess(query, cols, rows)
            return {"sql": sql_used, "columns": cols, "data": rows}
        except Exception as e:
            return {"error": str(e), "sql": None, "columns": [], "data": []}

    @staticmethod
    def _run(db_path, sql):
        """SQL 실행 헬퍼. 실패해도 예외를 던지지 않고 (cols, rows, sql, error)
        로 돌려줘서 호출부가 재시도 여부를 판단할 수 있게 한다."""
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.close()
            return cols, rows, sql, None
        except Exception as e:
            return [], [], sql, str(e)
