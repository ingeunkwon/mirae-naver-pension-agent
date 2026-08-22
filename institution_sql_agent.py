#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
제도 SQL Agent — Text-to-SQL 없이 슬롯 조회 + 조건 필터로 동작한다.

왜 Text-to-SQL을 안 쓰는가
  제도 fact는 롱포맷 테이블 하나에 수백 행 규모다. 이걸 위해 LLM에게 SQL을
  짜게 하면 (1) 호출 비용, (2) 잘못된 SQL 생성, (3) 응답 지연이 전부
  새 실패 모드로 들어온다. 질의어에서 pension_type과 category를 뽑는 건
  규칙으로 되므로 슬롯 조회로 끝난다. HCX 호출 0회.

조회 순서 (v3에서 고침)
  v2는 [SQL LIMIT] → [조건 필터] → [LIMIT] 이었다. category가 둘 이상
  잡히면 SQL 단계의 LIMIT을 앞 category가 다 먹어서, 정작 필요한 category의
  fact가 후보에 들어오지도 못했다.
      예) '연금수령 + 과세'가 함께 잡힌 질문에서 과세 fact가 30개를 채우면
          연금수령 fact는 조회조차 안 됨
  v3는 [관련 row 전체 조회] → [조건 필터] → [적합도 정렬] → [LIMIT] 이다.
  DB가 수백 행 규모인 동안은 이게 안전하다. 안전 상한(HARD_CAP)만 둔다.

정렬 (v4에서 순서를 바꿈)
  0  **질문이 직접 지목한 항목**       ← deterministic item alias (신규 1순위)
  1  판정이 확정된 fact
  2  조건 일치 계층 (전부맞음 / 조건없음 / 일부맞음 / 전부불명)
  '판정 가능한 fact를 앞세운다'가 틀린 게 아니라 **그걸 1순위로 쓴 게** 틀렸다.
      'IRP 가입 3년인데 납입한도 얼마야?'
      → 적립기간 5년 요건이 (3<5로) 판정되어 1순위, 정작 물어본 납입한도가 뒤로
  질문이 항목을 직접 지목했으면 그게 먼저다. 순서만 바꾸고 **목록에서 빼지 않는다** —
  잘못 좁히면 정답을 아예 못 보지만, 안 좁히면 순위만 밀린다.

요건 판정 (v4 신규)
  met / unmet / unknown 3치다. Kleene 논리를 그대로 쓴다 (tri.py).
      55세 AND (가입 5년 OR 이연퇴직소득)  에 55세/3년/이연퇴직소득 미지정 →
      TRUE AND (FALSE OR UNKNOWN) = TRUE AND UNKNOWN = UNKNOWN
  v3은 OR 그룹이 UNKNOWN이면 그냥 '모름'으로 흘려보내고 value_subject(나이)만
  보고 **met**으로 판정했다. 일부 조건 하나가 충족됐다는 이유로 전체를 met으로
  판정하면 안 된다.
"""
from __future__ import annotations
import json, os, re, sqlite3, sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from safe_eval import safe_eval, UnsafeExpression  # noqa: E402
import tri  # noqa: E402
from tri import MET, UNMET, UNKNOWN  # noqa: E402

# 원본(Kwonjunil/mirae_asset_competiton)의 중첩 경로 대신 flat 레이아웃에 맞춘 경로.
ROOT = os.environ.get("PENSION_ROOT") or HERE
DEFAULT_DB = os.environ.get("PENSION_DB") or \
    os.path.join(ROOT, "data", "pension_rules.db")

HARD_CAP = 500          # 조회 안전 상한. DB가 커지면 이 값을 넘는지 로그에 남는다

# '공통'/'퇴직연금_공통'의 적용 범위는 **DB의 pension_type_groups가 갖는다.**
# v3까지는 이 파일과 validate_facts.py와 schema.sql 주석이 각자 다른 리스트를
# 들고 있었다 (여기: IRP·DC·DB·과학기술인연금·디폴트옵션 / 주석: DB·DC·IRP).
# 어긋나면 Agent가 근거로 쓴 fact를 Validator가 검사하지 않는다.
# 아래는 DB를 못 읽는 경우(단위 테스트 등)의 최후 기본값일 뿐이다.
_GROUP_FALLBACK = {
    "퇴직연금_공통": {"IRP", "DC", "DB"},
    "공통": {"IRP", "DC", "DB", "연금저축계좌", "개인연금저축_구",
             "과학기술인연금", "디폴트옵션", "ISA_연계"},
}


def load_type_groups(con) -> dict[str, set[str]]:
    """group_code → 적용 제도 집합. Agent와 Validator가 같은 함수를 쓴다."""
    try:
        rows = con.execute("SELECT group_code, member_type "
                           "FROM pension_type_groups").fetchall()
    except sqlite3.Error:
        return {k: set(v) for k, v in _GROUP_FALLBACK.items()}
    out: dict[str, set[str]] = {}
    for g, m in rows:
        out.setdefault(g, set()).add(m)
    return out or {k: set(v) for k, v in _GROUP_FALLBACK.items()}

# ---------------------------------------------------------------- 제도 탐지
# 주의: '개인연금저축'은 문자열에 '연금저축'을 포함한다.
# 그대로 두면 현행 연금저축계좌와 폐지된 구 개인연금저축이 함께 잡혀서
# 한 답변에 두 제도의 규칙이 섞인다. 부정 후방탐색으로 갈라준다.
_PT_PATTERNS = [
    ("IRP",            r"IRP|아이알피|개인형\s*퇴직연금"),
    ("DC",             r"(?<![A-Za-z])DC(?![A-Za-z])|확정기여"),
    ("DB",             r"(?<![A-Za-z])DB(?![A-Za-z])|확정급여"),
    ("연금저축계좌",    r"(?<!개인)(?<!구\s)연금저축"),
    ("개인연금저축_구", r"개인\s*연금저축|구\s*개인\s*연금|(?:'?0[0-9]|2000)년\s*이전.{0,10}개인연금"),
    ("과학기술인연금",  r"과학기술인연금|과기공|과학기술인공제"),
    ("ISA_연계",       r"(?<![A-Za-z])ISA(?![A-Za-z])|아이에스에이|만기\s*전환"),
    ("디폴트옵션",      r"디폴트\s*옵션|사전지정운용"),
]

# ---------------------------------------------------------------- 구분 탐지
# '이전'은 '2013년 이전'처럼 시점을 뜻할 때가 많다. 그 경우까지 이전전환으로
# 잡으면 엉뚱한 fact가 근거로 붙는다. (KB v6 문서에도 같은 오분류가 기록돼 있다)
_CAT_PATTERNS = [
    ("세액공제",  r"세액\s*공제|공제\s*율|공제\s*한도|절세액?|연말정산|소득공제|"
                 r"(?<!소득)공제\s*(?:한도|얼마|금액|되는)|세금\s*(?:혜택|아낄)"),
    ("납입한도",  r"납입\s*한도|불입\s*한도|얼마까지\s*(?:납입|넣|입금)|입금\s*한도|"
                 r"부담금|적립\s*한도|납입\s*가능\s*금액|얼마\s*(?:나\s*)?(?:넣|납입|불입)|"
                 r"(?:1년|연간|한\s*해)에?\s*얼마|넣을\s*수\s*있"),
    ("중도인출",  r"중도\s*인출|중간\s*정산|해지|담보\s*대출|압류|인출\s*사유|"
                 r"(?<!계약)\s*인출|(?:돈|자금|금액)\s*(?:을|를)?\s*(?:중간에\s*)?"
                 r"[빼뺄뽑찾]|[빼뺄]는\s*사유|출금"),
    ("연금수령",  r"연금\s*수령|연금\s*개시|수령\s*한도|개시\s*연령|몇\s*살|"
                 r"수령\s*기간|연금(?:을|를|으로)?\s*(?:얼마까지\s*)?받|수령\s*요건|수령\s*가능|"
                 r"받을\s*수\s*있는\s*나이|언제부터\s*(?:받|수령)"),
    # '퇴직소득'만으로 과세를 잡으면 '이연퇴직소득이 있는데 연금 받을 수 있어?'가
    # 세금 질문으로 오분류돼 1,500만원 종합과세 fact가 근거에 붙는다.
    # 세금 문맥이 함께 있을 때만 과세로 본다.
    # '퇴직소득'만으로 과세를 잡으면 '이연퇴직소득이 있는데 연금 받을 수 있어?'가
    # 세금 질문으로 오분류된다. 그렇다고 `퇴직소득 ... 세` 로 열어두면
    # '이연퇴직소득이 없는데 55**세**부터'의 55세까지 걸린다.
    # 세금 어휘 자체를 요구한다.
    ("과세",      r"세금|세율|과세|소득세|기타소득|분리과세|종합과세|비과세|원천징수|"
                 r"퇴직소득세|퇴직소득[^.?!\n]{0,6}(?:과세|세율|세금|원천징수)"),
    ("이전전환",  r"계약\s*이전|계좌\s*이전|실물\s*이전|의무\s*이전|"
                 r"이전\s*(?:신청|가능|여부|절차|방법|하려|하면|할)|"
                 r"이체|전환|승계|이관|수관|옮[기길겼]|갈아타"),
    ("운용",      r"운용|투자\s*한도|위험\s*자산|비중|매매|상품\s*선택|TDF|"
                 r"디폴트\s*옵션|사전지정운용"),
    ("가입대상",  r"가입\s*대상|가입\s*자격|가입할\s*수|누가\s*가입|연령\s*제한|"
                 r"가입\s*조건|아무나\s*(?:가입|만들|개설)|만들\s*수\s*있|"
                 r"개설\s*(?:가능|자격|조건)"),
]

# '되나요', '가능한가' 같은 일반 어미는 넣지 않는다.
# 넣으면 거의 모든 질문이 SQL 경로를 타서, 관련 없는 fact가 근거로 섞인다.
_SQL_SIGNAL = re.compile(
    r"몇\s*(개|살|년|퍼센트|%)|얼마|한도|최대|최소|가장\s*(싼|낮|높|큰)|"
    r"이하|이상|미만|초과|비교|차이|등급|세율|공제율|요건|자격|기준")

# ---------------------------------------------------------------- 조건 탐지
_AMOUNT = r"([\d,]+(?:\.\d+)?)\s*(억|천만|백만|만|천)?\s*원"
_COND_EXTRACT = [
    ("total_salary",          rf"(?:총\s*급여|연봉|연간\s*급여)[^\d]{{0,10}}{_AMOUNT}"),
    ("gross_income",          rf"종합\s*소득(?:금액)?[^\d]{{0,10}}{_AMOUNT}"),
    ("annual_pension_income", rf"연금\s*소득[^\d]{{0,10}}{_AMOUNT}"),
    ("severance_amount",      rf"퇴직\s*(?:금|급여)[^\d]{{0,10}}{_AMOUNT}"),
    ("account_value",         rf"(?:평가액|적립금|잔고|적립액|계좌\s*금액)[^\d]{{0,10}}{_AMOUNT}"),
    ("medical_cost",          rf"(?:의료비|간병비|치료비)[^\d]{{0,10}}{_AMOUNT}"),
    ("annual_wage_total",     rf"(?:연간\s*임금\s*총액|임금\s*총액|연간\s*총\s*임금)"
                              rf"[^\d]{{0,10}}{_AMOUNT}"),
]

# 중도인출 사유(범주형). fact_conditions의 withdrawal_reason 토큰과 어휘를 공유한다.
_REASON_PATTERNS = [
    ("home_purchase",          r"주택\s*구입|집을?\s*(사|삼|구입)|내\s*집\s*마련|주택\s*매입"),
    ("jeonse_deposit",         r"전세|임차\s*보증금|주거.{0,4}보증금|월세\s*보증금"),
    ("long_term_care",         r"요양|치료|입원|간병"),
    # 금융회사 파산이 개인 파산으로 잡히지 않도록 기관 사유를 먼저 본다
    ("institution_suspension", r"영업\s*정지|영업\s*인가|(?:금융회사|저축기관|사업자)[^.?!\n]{0,6}파산"),
    ("bankruptcy",             r"파산|개인\s*회생"),
    ("natural_disaster",       r"천재지변|재난"),
    ("loan_repayment",         r"담보\s*대출.{0,8}상환|대출\s*원리금"),
    ("death_or_emigration",    r"사망|해외\s*이주"),
]

_TRANSFER_PATTERNS = [
    ("IRP<->연금저축계좌", r"IRP.{0,12}연금저축|연금저축.{0,12}IRP"),
    ("IRP<->IRP",         r"IRP.{0,8}(상호|간|끼리|다른\s*IRP)|IRP\s*↔\s*IRP"),
]

_AGE = re.compile(r"(?:만\s*)?(\d{1,3})\s*세")
# 'N년차' 단독을 연금수령연차로 보면 안 된다.
#   'IRP 가입 3년차이고 평가액 1억원인데 연금수령 한도는?' 을 payout_year=3으로
#   읽고 연금수령한도 15,000,000원을 실제로 계산해버렸다. 가입 3년차인데.
_PAYOUT_STRONG = re.compile(
    r"(?:연금\s*)?수령\s*(?:개시\s*)?(\d{1,2})\s*년\s*차"
    r"|연금\s*(?:을|를)?\s*받(?:은|는)\s*지\s*(\d{1,2})\s*년\s*차"
    r"|연금\s*개시\s*(?:후\s*)?(\d{1,2})\s*년\s*차")
_YEARCHA_PLAIN = re.compile(r"(\d{1,2})\s*년\s*차")
_PAYOUT_CTX = re.compile(r"연금\s*수령|수령\s*한도|연금\s*개시|연금\s*받")
_CARE_MONTHS = re.compile(
    r"(?:요양|치료|입원|간병)\s*(?:기간\s*)?(\d{1,3})\s*개?\s*월"
    r"|(\d{1,3})\s*개?\s*월\s*(?:이상\s*)?(?:요양|치료|입원|간병)")
_CONTRIB_YEARS = re.compile(
    r"(?:가입|적립|납입)\s*(?:한\s*지|기간(?:이)?)?\s*(\d{1,2})\s*년"      # 가입한 지 3년
    r"|(\d{1,2})\s*년\s*(?:째|간|동안)?\s*(?:가입|적립|납입|넣|불입)"       # 3년 가입했어
    r"|(?:가입|적립|납입)\s*기간\s*(?:이)?\s*(\d{1,2})\s*년")
_LEAVE_MONTHS = re.compile(r"휴직\s*(?:기간\s*)?(\d{1,3})\s*개?\s*월")
# 연금계좌 최초 가입 연도. '2013년 3월 이전에 가입'처럼 경계를 직접 말하는 표현도 받는다.
_OPEN_YEAR = re.compile(
    r"(?:최초\s*)?가입(?:일|한\s*(?:것|게|건))?\s*(?:이|은|가)?\s*"
    r"(?:20|19)(\d{2})\s*년"
    r"|(?:20|19)(\d{2})\s*년\s*(?:\d{1,2}\s*월\s*)?에?\s*(?:최초\s*)?가입")
_OPEN_BEFORE_2013 = re.compile(
    r"2013\s*년\s*3\s*월\s*(?:1\s*일\s*)?이전|2013\.\s*3\.\s*1\s*이전|"
    r"2013\s*년\s*이전\s*가입|구\s*연금저축")
_OPEN_AFTER_2013 = re.compile(
    r"2013\s*년\s*3\s*월\s*(?:1\s*일\s*)?이후|2013\.\s*3\.\s*1\s*이후")
_DEPOSIT_YEARS = re.compile(r"(?:퇴직금|이연퇴직소득)\s*(?:입금|납입).{0,8}?(\d{1,2})\s*년")

# 불리언·토큰 조건은 **부정을 먼저** 확정한다.
#   v3까지는 긍정 패턴을 먼저 보고 `if key in out: continue` 했기 때문에
#   '이연퇴직소득이 없는데' → has_deferred_severance=1,
#   '비거주자'            → is_resident=1 ('비거주자'가 '거주자'를 포함),
#   'MP 구독 안 하는데'    → service='MP구독' (68% fact가 1순위로 올라옴),
#   'ISA 만기전환 안 하면' → has_isa_rollover=1
#   처럼 뜻이 정반대로 뒤집혔다. 최종 답변이 실제로 틀리는 오류였다.
# (key, 부정 정규식, 부정값, 긍정 정규식, 긍정값)
_NEG_TAIL = r"[^.?!\n]{0,14}?(?:안\s*(?:하|해|했|받|쓰)|하지\s*않|않(?:습니다|아|은|는)|" \
            r"미\s*(?:보유|이용|구독|가입)|없(?:는|다|어|습니다|이|을)|아닙|아니|제외|해지)"
_BOOL_PATTERNS = [
    ("has_deferred_severance",
     rf"이연\s*퇴직(?:금|소득){_NEG_TAIL}|퇴직금\s*(?:이|은)?\s*없", 0,
     r"이연\s*퇴직(?:금|소득)|퇴직금\s*(?:이|을|은)?\s*(?:있|보유|들어|입금)", 1),
    ("is_resident",
     r"비\s*거주자|거주자가?\s*아닌|해외\s*거주", 0,
     r"(?<!비)(?<!비\s)거주자", 1),
    ("has_isa_rollover",
     rf"(?:ISA|아이에스에이|만기\s*전환){_NEG_TAIL}|ISA\s*(?:는|가|를)?\s*없", 0,
     r"ISA[^.?!\n]{0,10}(?:만기|전환|이전)|만기\s*ISA|ISA\s*전환", 1),
    ("service",
     rf"(?:MP\s*구독|구독\s*서비스){_NEG_TAIL}|미\s*구독|구독\s*안", "MP미구독",
     r"MP\s*구독|구독\s*서비스", "MP구독"),
    ("is_homeless",
     r"유\s*주택|집(?:이|을)?\s*(?:있|보유|소유)|1\s*주택|주택\s*보유|자가", 0,
     r"무\s*주택", 1),
    ("ownership",
     r"(?:배우자|부모|자녀|타인|공동)\s*명의", "other",
     r"본인\s*명의|내\s*명의", "self"),
    ("product_type",
     None, None,
     r"(?<![A-Za-z])TDF(?![A-Za-z])|타깃\s*데이트|타겟\s*데이트", "TDF"),
]

# 조건 키로 category를 유추한다.
# category가 안 잡힐 때 DB 전체를 뒤지면 'IRP 최대 얼마야?'에 16.5% / 1,800만원 /
# 5년 / 900만원 / 55세 / 70% 가 한꺼번에 근거로 붙는다. 그건 [확정 수치] 블록을
# 오염시켜 생성기가 엉뚱한 숫자를 인용하게 만든다.
# 조건 키가 가리키는 구분이 명확한 것만 유추하고, age·contribution_years처럼
# 여러 구분에 걸치는 것은 유추하지 않는다.
_KEY_CATEGORY = {
    "withdrawal_reason": "중도인출", "statutory_reason": "중도인출",
    "tax_hardship_reason": "중도인출", "medical_cost": "중도인출",
    "leave_months": "중도인출",
    "has_isa_rollover": "세액공제", "total_salary": "세액공제",
    "gross_income": "세액공제",
    "payout_year": "연금수령", "account_value": "연금수령",
    "service": "운용", "product_type": "운용",
    "transfer_direction": "이전전환",
    "severance_deposit_years": "과세", "annual_pension_income": "과세",
    "is_resident": "가입대상",
}

_UNIT_MUL = {None: 1, "천": 1_000, "만": 10_000, "백만": 1_000_000,
             "천만": 10_000_000, "억": 100_000_000}


def _to_won(num: str, unit: str | None) -> float:
    return float(num.replace(",", "")) * _UNIT_MUL.get(unit, 1)


# '5천5백만원'처럼 복합 단위는 정규식 하나로 못 읽는다. 먼저 평평하게 편다.
_COMPOUND = [
    (re.compile(r"(\d+)\s*억\s*(\d+)\s*천\s*만\s*원"),
     lambda m: f"{int(m.group(1)) * 10000 + int(m.group(2)) * 1000}만원"),
    (re.compile(r"(\d+)\s*천\s*(\d+)\s*백\s*만\s*원"),
     lambda m: f"{int(m.group(1)) * 1000 + int(m.group(2)) * 100}만원"),
    (re.compile(r"(\d+)\s*천\s*(\d+)\s*십\s*만\s*원"),
     lambda m: f"{int(m.group(1)) * 1000 + int(m.group(2)) * 10}만원"),
]
# '5천만원이 아니라 7천만원' — 대조 표현이 있으면 뒤쪽 값이 현재 값이다.
_CONTRAST = re.compile(r"아니라|아니고|말고|에서\s*$|(?:으로|로)\s*(?:올랐|늘었|바뀌|변경)")


def normalize_amounts(q: str) -> str:
    for pat, fn in _COMPOUND:
        q = pat.sub(fn, q)
    return q


def detect_pension_types(q: str) -> list[str]:
    return [pt for pt, pat in _PT_PATTERNS if re.search(pat, q, re.I)]


def detect_categories(q: str) -> list[str]:
    return [c for c, pat in _CAT_PATTERNS if re.search(pat, q)]


def detect_conditions(q: str) -> dict[str, Any]:
    """질의어에서 조건 값을 뽑는다. 못 뽑으면 넣지 않는다 — 그러면 필터를 안 건다."""
    q = normalize_amounts(q)
    out: dict[str, Any] = {}
    amount = re.compile(_AMOUNT)
    for key, pat in _COND_EXTRACT:
        m = re.search(pat, q)
        if not m:
            continue
        val = _to_won(m.group(1), m.group(2))
        # '총급여가 5천만원이 아니라 7천만원인데' — 대조 뒤의 값이 현재 값이다
        tail = q[m.end(): m.end() + 40]
        if _CONTRAST.search(tail):
            after = amount.search(tail[_CONTRAST.search(tail).end():])
            if after:
                val = _to_won(after.group(1), after.group(2))
        out[key] = val

    # 평가액은 실제 질문에서 "1억원"뿐 아니라 "1억"처럼 '원'을 생략하는 경우가 잦다.
    # 일반 금액 정규식 전체를 느슨하게 만들면 연차·비율 같은 숫자를 금액으로 오인할 수 있으므로
    # account_value에만 제한된 fallback을 둔다. (원칙 B: parser 보강)
    if "account_value" not in out:
        m = re.search(
            r"(?:평가액|적립금|잔고|적립액|계좌\s*금액)[^\d]{0,10}"
            r"([\d,]+(?:\.\d+)?)\s*(억|천만|백만|만|천)\s*(?:원)?",
            q)
        if m:
            out["account_value"] = _to_won(m.group(1), m.group(2))

    for key, pat in (("age", _AGE),
                     ("contribution_years", _CONTRIB_YEARS),
                     ("leave_months", _LEAVE_MONTHS),
                     ("care_months", _CARE_MONTHS),
                     ("severance_deposit_years", _DEPOSIT_YEARS)):
        m = pat.search(q)
        if m:
            # 대안(|)이 여럿인 정규식은 매칭된 그룹만 값이 있고 나머지는 None이다
            g = next((x for x in m.groups() if x is not None), None)
            if g is not None:
                out[key] = float(g)
    # --- 연금수령연차: 수령 문맥이 있을 때만
    m = _PAYOUT_STRONG.search(q)
    if m:
        out["payout_year"] = float(next(x for x in m.groups() if x is not None))
    else:
        cm = _CONTRIB_YEARS.search(q)
        span = cm.span() if cm else None
        pm = _YEARCHA_PLAIN.search(q)
        overlaps = span and not (pm.end() <= span[0] or pm.start() >= span[1]) if pm else False
        if pm and not overlaps and _PAYOUT_CTX.search(q):
            out["payout_year"] = float(pm.group(1))
    for token, pat in _REASON_PATTERNS:
        if re.search(pat, q):
            out["withdrawal_reason"] = token
            break
    for token, pat in _TRANSFER_PATTERNS:
        if re.search(pat, q, re.I):
            out["transfer_direction"] = token
            break
    for key, neg_pat, neg_val, pos_pat, pos_val in _BOOL_PATTERNS:
        if key in out:
            continue
        # 부정을 먼저 본다. 부정이 걸리면 긍정은 아예 검사하지 않는다.
        if neg_pat and re.search(neg_pat, q, re.I):
            out[key] = neg_val
            continue
        if pos_pat and re.search(pos_pat, q, re.I):
            out[key] = pos_val

    # --- 가입 시점 → 2013.3.1 경계
    # 연도만 알면 2013년은 3월 경계를 가를 수 없다. **추측하지 않고 비워 둔다** —
    # 그러면 Tri가 unknown으로 처리하고, 필요하면 되묻는다.
    m = _OPEN_YEAR.search(q)
    if m:
        g = next((x for x in m.groups() if x is not None), None)
        if g is not None:
            year = 2000 + int(g) if int(g) < 50 else 1900 + int(g)
            # 정규식이 '20'/'19' 접두를 이미 소비했으므로 두 자리만 남는다
            year = int(re.search(r"(?:20|19)\d{2}", m.group()).group())
            out["account_open_year"] = float(year)
            if year <= 2012:
                out["opened_before_2013_03"] = 1
            elif year >= 2014:
                out["opened_before_2013_03"] = 0
            # 2013년은 판정 불가 — 넣지 않는다
    if _OPEN_BEFORE_2013.search(q):
        out["opened_before_2013_03"] = 1
    elif _OPEN_AFTER_2013.search(q):
        out["opened_before_2013_03"] = 0

    # --- 유도 조건: 의료비 / 연간 임금총액 비율
    # 조건 모델은 (키, 연산자, 상수)뿐이라 키끼리 비교할 수 없다.
    # 구조를 바꾸는 대신 **파서가 비율을 계산해** 상수 비교로 만든다.
    # (BASELINE_POLICY.md 원칙 B — 표현 불가가 아니라 파서 보강 문제였다)
    if out.get("annual_wage_total") and out.get("medical_cost") is not None:
        out["medical_cost_ratio"] = round(
            100.0 * out["medical_cost"] / out["annual_wage_total"], 4)
    return out


# 구분(category)을 못 잡았을 때, 조건 키로 구분을 유추한다.
# 여러 구분에 걸치는 키(age, contribution_years)는 유추에 쓰지 않는다.
CONDITION_CATEGORY_HINT = {
    "withdrawal_reason": ["중도인출", "과세"],
    "total_salary": ["세액공제"],
    "gross_income": ["세액공제"],
    "has_isa_rollover": ["세액공제"],
    "annual_pension_income": ["과세"],
    "payout_year": ["연금수령", "과세"],
    "account_value": ["연금수령"],
    "transfer_direction": ["이전전환"],
    "severance_amount": ["이전전환"],
    "severance_deposit_years": ["과세"],
    "service": ["운용"],
    "product_type": ["운용"],
    "is_resident": ["가입대상"],
    "medical_cost": ["중도인출"],
    "leave_months": ["중도인출"],
    "care_months": ["중도인출"],
    "is_homeless": ["중도인출"],
    "ownership": ["중도인출"],
}


def needs_sql(q: str) -> bool:
    return bool(_SQL_SIGNAL.search(q)) or bool(detect_categories(q))


# --------------------------------------------------------------- 조건 평가
_CMP = {"=": lambda a, b: a == b, ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b, "<=": lambda a, b: a <= b,
        "<": lambda a, b: a < b}


def condition_holds(cond, given: dict) -> bool | None:
    """주어진 값으로 조건이 성립하는지. 판단할 수 없으면 None(=배제하지 않음)."""
    key = cond["condition_key"]
    if key not in given:
        return None                      # 모르면 거르지 않는다
    v = given[key]
    op = cond["condition_op"]
    tok = cond["condition_token"]
    if tok is not None:
        # cardinality='multi' 키는 값이 **집합**으로 올 수 있다 (상품 유형 등).
        # 스키마와 Validator는 multi를 알고 있는데 여기만 스칼라 비교였다.
        # 그러면 검사기는 '동시 보유 가능'이라 하고 Agent는 배타로 판정해,
        # 같은 데이터에 대해 두 모듈이 다른 답을 낸다. (랜덤 검사 F7이 잡았다)
        if isinstance(v, (list, tuple, set, frozenset)):
            held = {str(x) for x in v}
            if op == "is":
                return tok in held
            if op == "is_not":
                return tok not in held
            return None
        if op == "is":
            return str(v) == tok
        if op == "is_not":
            return str(v) != tok
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None                      # 수치 조건에 비수치가 들어온 경우
    num = cond["condition_num"]
    if num is None:
        return None
    if op == "between":
        return num <= v <= cond["condition_num_max"]
    if op in _CMP:
        return _CMP[op](v, num)
    return None


class CondResult:
    """조건 평가 결과. 3치 판정과 '무엇을 몰라서 모르는지'를 함께 들고 다닌다."""

    __slots__ = ("drop", "status", "unmet", "unknown_keys", "n_true", "n_unknown")

    def __init__(self, drop, status, unmet, unknown_keys, n_true, n_unknown):
        self.drop = drop                  # selector가 어긋남 → 이 질문의 답이 아니다
        self.status = status              # met | unmet | unknown | None(미판정)
        self.unmet = unmet                # 확정 미충족 requirement 조건행
        self.unknown_keys = unknown_keys  # 판정에 필요한데 질문에 없던 조건 키
        self.n_true = n_true
        self.n_unknown = n_unknown


def _group_verdict(rows, given):
    """한 condition_group의 3치 판정과, UNKNOWN을 만든 조건 키들.

    group 0  각 조건이 독립(AND) — 호출자가 행 단위로 부른다
    group >0 같은 그룹끼리 OR
    """
    verdicts, unknown_keys = [], []
    for c in rows:
        v = tri.from_bool(condition_holds(c, given))
        verdicts.append(v)
        if v == UNKNOWN:
            unknown_keys.append(c["condition_key"])
    return verdicts, unknown_keys


def evaluate_conditions(conds, given, extra_observed: bool = False) -> CondResult:
    """조건을 3치로 평가한다.

    selector    분기 조건. 어긋나면 이 fact는 이 질문의 답이 아니다 → 제외(drop).
                **판단할 수 없으면 제외하지 않는다.** 잘못 좁히면 정답을 아예 못
                보지만, 안 좁히면 순위만 밀린다.
    requirement 자격 요건. 어긋나도 제외하지 않는다 — 오히려 그 fact가
                '요건을 충족하지 못했다'는 답의 근거다.

    ★ v3에서 고친 곳: OR 그룹이 UNKNOWN일 때
        v3은 `n_unknown += 1`로 흘려보내고 requirement 판정에서 빠뜨렸다.
        그래서 age>=55 AND (가입 5년 OR 이연퇴직소득) 에
        '55세 / 가입 3년 / 이연퇴직소득 미지정'을 넣으면
        OR 그룹은 무시되고 나이만 보고 **met**이 나왔다.
        Kleene으로는 FALSE OR UNKNOWN = UNKNOWN 이고,
        TRUE AND UNKNOWN = UNKNOWN 이다.

    필터 의미론과 주장 의미론을 분리한다.
        필터: unknown → 행을 배제하지 않는다   (기존 동작 유지)
        판정: unknown → met이라고 말하지 않는다 (신규)
    같은 값을 두 군데서 다르게 쓰는 게 정상이다. 하나로 합치면 둘 중 하나가 망가진다.
    """
    groups: dict[int, list] = {}
    for c in conds:
        groups.setdefault(c["condition_group"], []).append(c)

    # 질문이 이 fact를 **건드렸는가**. 역할과 무관하게 조건 키 하나라도 관측되면
    # 이 fact는 질문과 관련이 있다. None(미판정)과 UNKNOWN을 가르는 기준이다.
    #   '요양으로 중도인출 되나요?' → withdrawal_reason 관측됨(selector)
    #   → 유일한 요건 care_months가 미관측이어도 None이 아니라 UNKNOWN이어야 한다
    observed = bool(extra_observed) or any(c["condition_key"] in given for c in conds)

    drop = False
    unmet: list = []
    n_true = n_unknown = 0
    components: list[tuple[str, list[str]]] = []   # requirement 성분 (tri 합성용)

    for g, rows in sorted(groups.items()):
        if g == 0:
            units = [[c] for c in rows]        # 각자 독립 AND
        else:
            units = [rows]                     # 그룹 전체가 하나의 OR 단위

        for unit in units:
            verdicts, unknown_keys = _group_verdict(unit, given)
            verdict = verdicts[0] if len(unit) == 1 else tri.or_(verdicts)

            # 한 그룹에 역할이 섞이면 requirement가 이긴다.
            # 자격 요건을 selector로 취급해 근거를 지우는 쪽이 훨씬 해롭다.
            is_req = any(c["condition_role"] == "requirement" for c in unit)

            if verdict == MET:
                n_true += 1
            elif verdict == UNKNOWN:
                n_unknown += 1
            elif not is_req:
                drop = True                    # selector 확정 불일치 → 제외

            if is_req:
                components.append((verdict, unknown_keys))
                if verdict == UNMET:
                    unmet.extend(unit)

    status = tri.combine_components(components, observed)
    return CondResult(drop, status, unmet, tri.missing_keys(components),
                      n_true, n_unknown)


def evaluate_value(row, given) -> str | None:
    """value_subject가 있으면 질의값과 value_op를 직접 비교한다.

    '54세인데 IRP 연금 받을 수 있어?' → 개시연령 >= 55 를 54로 판정 → 'unmet'.
    SQL Agent가 여기까지 확정해줘야 생성 단계가 추측하지 않는다.

    반환은 met / unmet / None이다. **None은 '비교할 수 없음'이지 met이 아니다.**
    fact_role='threshold'가 아니면 비교 자체를 하지 않는다 — 연금수령연차 기산
    시작값(=6) 같은 파라미터를 임계값으로 비교하면 7년차가 미충족이 된다.
    """
    if row["fact_role"] != "threshold":
        return None
    subj = row["value_subject"]
    if not subj or subj not in given:
        return None
    v = given[subj]
    # 수치 비교이므로 문자열·집합은 비교 대상이 아니다 (multi 키 값이 올 수 있다)
    if not isinstance(v, (int, float)) or isinstance(v, bool) \
            or row["value_num"] is None:
        return None
    op, n, mx = row["value_op"], row["value_num"], row["value_num_max"]
    if op == "between":
        return MET if n <= v <= mx else UNMET
    if op in _CMP:
        return MET if _CMP[op](v, n) else UNMET
    return None


def merge_status(cond_status: str | None, value_status: str | None) -> str | None:
    """조건 판정과 value_subject 판정을 Kleene AND로 합친다.

    '55세 + 가입 3년 + 이연퇴직소득 미지정'에서
        value_subject(age>=55) = met
        조건(가입 5년 OR 이연퇴직소득) = unknown
    → and_(met, unknown) = **unknown**. 여기서 met이 나오면 안 된다.
    """
    comps = [s for s in (cond_status, value_status) if s is not None]
    if not comps:
        return None
    return tri.and_(comps)


# 정렬 계층
TIER_ALL_MATCH, TIER_NO_COND, TIER_PARTIAL, TIER_UNKNOWN = 0, 1, 2, 3

_TOKEN_RE = re.compile(r"[0-9]+(?:[.,][0-9]+)*|[A-Za-z]+|[가-힣]+")


ITEM_RELEVANT = 2.0      # 이 값 이상이면 '질문이 이 항목을 직접 가리킨다'로 본다


# ---------------------------------------------------- deterministic item intent
# 사용자가 **직접 물은 항목**을 결정적으로 집는다. 어휘 점수(_lexical_score)는
# 같은 계층 안의 tie-break용이라 계층이 다르면 무력하다. 이건 계층 자체를 이긴다.
#
#   'IRP 가입 3년인데 납입한도 얼마야?'
#     v3: 적립기간 5년 요건이 (3<5로) 판정 확정 → 1순위
#         납입한도 1,800만원은 판정할 게 없어 뒤로
#     v4: 질문이 '납입한도'를 지목 → 납입한도가 1순위, 적립기간 요건은 그 뒤
#
# 원칙 두 가지.
#   1. **가산점이지 필터가 아니다.** 지목되지 않은 fact를 목록에서 빼지 않는다.
#      'IRP 가입 3년 + 납입한도'에서 적립기간 5년 요건은 개시 요건으로 유용하다.
#      순서만 바꾼다.
#   2. 사전은 여기 한 곳에만 둔다. 구분(_CAT_PATTERNS)과 같은 파일에 두어
#      태깅·라우팅·정렬이 서로 어긋날 수 없게 한다.
#
# (intent, 질문 정규식, 해당 category 집합, item에 들어 있어야 하는 키워드)
ITEM_INTENTS: list[tuple[str, str, tuple, tuple]] = [
    ("납입한도", r"납입\s*한도|불입\s*한도|입금\s*한도|적립\s*한도|납입\s*가능\s*금액|"
                r"얼마까지\s*(?:납입|넣|입금)|(?:1년|연간|한\s*해)에?\s*얼마|"
                r"얼마\s*(?:나\s*)?(?:넣|납입|불입)|넣을\s*수\s*있",
     ("납입한도",), ("납입한도",)),
    ("세액공제한도", r"세액\s*공제\s*(?:한도|얼마|금액)|공제\s*한도|"
                   r"세액\s*공제.{0,6}(?:얼마|최대)",
     ("세액공제",), ("한도",)),
    ("공제율", r"공제\s*율|공제\s*비율|몇\s*%.{0,6}공제|공제.{0,4}몇\s*(?:%|퍼센트)",
     ("세액공제",), ("공제율",)),
    ("절세액", r"절세|얼마나?\s*아[낄껴]|환급",
     ("세액공제",), ("절세액",)),
    # '세금이 어떻게 돼?'는 세율을 묻는 가장 흔한 구어체다. 이걸 놓치면
    # 사유·서류 관련 fact가 세율 fact보다 앞에 온다. (원칙 B — 파서 보강)
    ("세율", r"세율|세금\s*(?:이|은|을)?\s*(?:얼마|몇|어떻게|어떨|어때)|"
            r"세금\s*(?:이|은|을)?\s*(?:얼마나\s*)?(?:나와|내|떼|붙)|"
            r"몇\s*(?:%|퍼센트).{0,6}세|과세\s*(?:율|비율)",
     ("과세",), ("세율",)),
    ("개시연령", r"개시\s*연령|몇\s*살|받을\s*수\s*있는\s*나이|언제부터\s*(?:받|수령)|"
                r"수령\s*(?:개시\s*)?나이",
     ("연금수령",), ("개시 연령", "가입 연령")),
    ("중도인출가능여부", r"중도\s*인출\s*(?:이|은|을)?\s*(?:가능|되나|돼|될까|할\s*수)|"
                      r"인출\s*가능\s*(?:여부|한가|해)|[빼뺄]\s*수\s*있",
     ("중도인출",), ("가능 여부",)),
    ("수령한도", r"수령\s*한도|연금\s*한도|한\s*해에?\s*받을\s*수\s*있는",
     ("연금수령",), ("한도",)),
    # ISA 만기자금을 연금계좌로 옮기는 질문은 세액공제와 이전전환이 함께 잡힌다.
    # fact가 늘어나면 '가입기간 3년' 같은 일반 전환요건이 앞 6개를 차지해
    # 정작 원문상 핵심 기한(60일)이 offline 답변에서 잘릴 수 있다.
    # 이건 필터가 아니라 순서만 올리는 B(ITEM_INTENTS) 보강이다.
    ("ISA전환기한",
     r"ISA.{0,20}(?:만기|만기자금).{0,30}(?:연금계좌|연금저축|IRP).{0,20}"
     r"(?:옮기|이체|전환|입금)|"
     r"ISA.{0,20}(?:연금계좌|연금저축|IRP).{0,20}(?:옮기|이체|전환|입금)",
     ("이전전환",), ("이전 기한",)),
    # IRP↔연금저축 이전 조건 질문은 일반적인 이전전환 fact가 늘어날수록
    # 정작 원문상 핵심 요건(55세·가입 5년·전액 이체)이 top-N 밖으로 밀릴 수 있다.
    # 특정 두 제도와 '조건/요건'을 함께 명시한 질문에만 좁게 적용한다.
    ("IRP연금저축이전요건",
     r"(?=.*IRP)(?=.*연금저축)(?=.*(?:옮기|이체|이전|전환))(?=.*(?:조건|요건|가능))",
     ("이전전환",), ("이체 요건",)),
]

_ITEM_INTENTS_C = [(name, re.compile(pat), cats, kws)
                   for name, pat, cats, kws in ITEM_INTENTS]


def detect_item_intents(question: str) -> list[str]:
    """질문이 직접 지목한 항목 intent들."""
    return [name for name, pat, _, _ in _ITEM_INTENTS_C if pat.search(question)]


def _intent_match(intents: list[str], row) -> bool:
    """이 fact가 질문이 지목한 항목인가."""
    if not intents:
        return False
    for name, _, cats, kws in _ITEM_INTENTS_C:
        if name not in intents:
            continue
        if row["category"] in cats and any(kw in row["item"] for kw in kws):
            return True
    return False


def _item_relevance(question: str, row) -> float:
    """질문이 이 fact의 **항목 이름**을 얼마나 직접 가리키는가 (어휘 기반 보조 점수).

    tier(조건 일치도)만으로 정렬하면 '중도인출 세율을 사유별로' 질문에서
    조건 없는 '증빙 제출 기한'이 조건부 '세율' fact보다 앞에 온다.
    조건을 판단할 수 없다(UNKNOWN)는 이유로 뒤로 밀리기 때문이다.
    질문이 항목을 직접 지목했다면 그게 우선이다.
    """
    return _lexical_score(question, {"item": row["item"], "condition_text": None})


def _lexical_score(question: str, row) -> float:
    """질문과 item/condition_text의 어휘 관련도. 결정적 계산이고 LLM을 쓰지 않는다.

    조건 일치도만으로 정렬하면 같은 계층 안에서 순서가 사실상 무작위다.
    96건에서는 1등이 맞아도, 300~500건이 되면 [확정 수치] 블록에 관련 없는
    숫자가 잔뜩 들어간다. 그래서 같은 계층 안에서만 쓰는 tie-breaker로 넣는다.
    """
    q = question.lower()
    target = f"{row['item']} {row['condition_text'] or ''}".lower()
    hit = 0.0
    for w in _TOKEN_RE.findall(target):
        if len(w) < 2:
            continue
        # 한국어는 조사가 붙으므로 접두어 일치까지 인정한다
        for L in range(len(w), 1, -1):
            if w[:L] in q:
                hit += L / len(w)      # 길게 겹칠수록 높게
                break
    return hit


class PensionSQLAgent:
    def __init__(self, db_path: str | None = None):
        self.db_path = os.path.abspath(db_path or DEFAULT_DB)
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"pension_rules.db가 없습니다: {self.db_path}")
        self.con = sqlite3.connect(self.db_path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        # 조건은 한 번만 읽어 메모리에 둔다 (fact당 쿼리 N+1 방지)
        self._conds: dict[str, list] = {}
        for c in self.con.execute("SELECT * FROM fact_conditions"):
            self._conds.setdefault(c["fact_id"], []).append(c)
        # 공통 코드의 적용 범위. 하드코딩하지 않고 DB에서 읽는다.
        self.type_groups = load_type_groups(self.con)
        self.retirement_types = self.type_groups.get("퇴직연금_공통", set())
        # 조건 키 라벨 — 되묻기 문구('이연퇴직금 보유 여부를 알려주시면')에 쓴다
        self.key_labels = {r["condition_key"]: r["label"] for r in
                           self.con.execute("SELECT condition_key, label "
                                            "FROM condition_keys")}

    def label_of(self, key: str) -> str:
        return self.key_labels.get(key, key)

    # -------------------------------------------------------------- 조회
    def lookup(self, pension_types: list[str], categories: list[str]) -> list:
        """관련 row를 **전부** 가져온다. LIMIT은 정렬 뒤에 건다."""
        where, params = [], []
        if pension_types:
            # '공통'은 항상 함께 본다.
            # '퇴직연금_공통'은 **퇴직연금 계열을 물었을 때만** 본다.
            #   연금저축계좌를 물었는데 퇴직연금 공통 규칙(세액공제 900만원)이
            #   따라붙으면, 연금저축 한도 600만원과 나란히 근거에 올라간다.
            #   (검사기 E7이 이 충돌을 잡아냈다)
            extra = ["공통"]
            if set(pension_types) & self.retirement_types:
                extra.append("퇴직연금_공통")
            pts = list(dict.fromkeys(list(pension_types) + extra))
            where.append("pension_type IN (%s)" % ",".join("?" * len(pts)))
            params += pts
        if categories:
            where.append("category IN (%s)" % ",".join("?" * len(categories)))
            params += categories
        sql = "SELECT * FROM pension_facts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 상한에 걸릴 때 '어떤 500개'인지가 실행마다 달라지면 안 된다.
        sql += " ORDER BY category, item, fact_id LIMIT ?"
        params.append(HARD_CAP)
        return self.con.execute(sql, params).fetchall()

    def conditions_of(self, fact_id: str) -> list:
        return self._conds.get(fact_id, [])

    def rank(self, rows: list, given: dict, question: str = ""):
        """selector가 어긋난 fact만 제거하고, 나머지를 적합도 순으로 정렬한다.

        반환은 (row, CondResult, value_status, status) 튜플이다.

        정렬 1순위는 **질문이 직접 지목한 항목**이다 (deterministic item alias).
        v3은 '판정이 확정된 fact'가 1순위여서
            'IRP 가입 3년인데 납입한도 얼마야?' → 적립기간 5년 요건이 앞
        이 됐다. 판정 확정은 2순위로 내린다. 지목되지 않은 fact를 **빼지는 않는다.**
        """
        intents = detect_item_intents(question)
        kept, dropped = [], []
        for r in rows:
            conds = self.conditions_of(r["fact_id"])
            # value_subject를 질문이 준 것도 '이 fact를 건드렸다'에 해당한다
            subj_seen = bool(r["value_subject"]) and r["value_subject"] in given
            cres = evaluate_conditions(conds, given, extra_observed=subj_seen)
            if cres.drop:
                dropped.append(r)
                continue
            vstatus = evaluate_value(r, given)
            status = merge_status(cres.status, vstatus)
            if not conds:
                tier = TIER_NO_COND
            elif cres.n_unknown == 0:
                tier = TIER_ALL_MATCH
            elif cres.n_true > 0:
                tier = TIER_PARTIAL
            else:
                tier = TIER_UNKNOWN
            intent_hit = 0 if _intent_match(intents, r) else 1
            decided = 0 if status in (MET, UNMET) else 1
            item_hit = 0 if _item_relevance(question, r) >= ITEM_RELEVANT else 1
            kept.append((intent_hit, decided, item_hit, tier, -cres.n_true,
                         r, cres, vstatus, status))

        order_time = {"current": 0, "unknown": 1, "mixed": 2, "historical": 3}
        order_conf = {"high": 0, "medium": 1, "low": 2}
        kept.sort(key=lambda x: (
            x[0], x[1], x[2], x[3], x[4],
            -round(_lexical_score(question, x[5]), 3),   # 같은 계층 안 tie-break
            0 if x[5]["valid_to"] is None else 1,
            order_time.get(x[5]["time_scope"], 9),
            order_conf.get(x[5]["confidence"], 9),
            x[5]["category"], x[5]["item"]))
        return [(r, c, v, s) for _, _, _, _, _, r, c, v, s in kept], dropped

    def _dedupe(self, ranked, pension_types):
        """**같은 규칙의 중복 provenance만** 정리한다.

        v4는 키가 (구분·항목·값)뿐이라 조건이 다른 fact까지 지웠다.
        'IRP 중도인출 세율을 사유별로' 질문에서 36개 중 19개가 삭제됐다 —
        주택구입/전세/요양/파산이 전부 '중도인출 가능'이라는 이유로.
        이제 **조건 서명이 같고, 공통 ↔ 개별 제도 관계일 때만** 합친다.
        같은 제도 안에서 조건이 다른 fact는 절대 합치지 않는다.
        """
        asked = set(pension_types or [])
        common = {"공통", "퇴직연금_공통"}
        seen, kept, dropped = {}, [], []
        for item in ranked:
            r = item[0]
            sig = tuple(sorted(
                (c["condition_key"], c["condition_op"], c["condition_num"],
                 c["condition_num_max"], c["condition_token"])
                for c in self.conditions_of(r["fact_id"])))
            key = (r["category"], r["item"], r["value_num"], r["value_num_max"],
                   r["value_unit"], r["value_bool"], sig)
            prev = seen.get(key)
            if prev is None:
                seen[key] = item
                kept.append(item)
                continue
            pr = prev[0]
            if not ({r["pension_type"], pr["pension_type"]} & common):
                kept.append(item)        # 서로 다른 개별 제도는 둘 다 남긴다
                continue
            if r["pension_type"] in asked and pr["pension_type"] not in asked:
                kept[kept.index(prev)] = item
                seen[key] = item
                dropped.append(pr)
            else:
                dropped.append(r)
        return kept, dropped

    def formulas_for(self, pension_types: list[str], categories: list[str],
                     given: dict, question: str = "") -> list[dict]:
        """category와 **질문이 실제로 계산을 요구하는지**까지 맞는 공식만 돌려준다.

        v2는 '연금수령' 질문에서만 공식을 조회해서 중도인출 공식이 DB에 있어도
        호출될 길이 없었다. 반대로 category만 맞으면 무조건 공식을 붙이는 것도 문제다.
        예: '72세 연금 세율?'에 연금수령한도 공식이 따라붙어 account_value/payout_year를
        되묻는 오염이 생긴다. 공식별로 좁은 계산 intent를 확인한다.
        """
        rows = self.con.execute("SELECT * FROM pension_formulas").fetchall()
        out = []
        q = question or ""
        for f in rows:
            if categories and f["applies_category"] not in categories:
                continue
            if pension_types and not any(pt in f["applies_to"].split(",")
                                         for pt in pension_types):
                continue

            # category가 맞아도 질문이 계산을 요구하지 않으면 공식은 후보에 넣지 않는다.
            # 이 필터는 '계산할지'만 정하고 fact 조회에는 영향을 주지 않는다.
            fid = str(f["formula_id"])
            if fid.startswith("fm_pension_limit"):
                # 세율·자격 같은 일반 연금수령 질문에는 공식을 붙이지 않는다.
                # 다만 평가액/수령연차처럼 계산 입력이 실제로 주어지고 질문에
                # '한도/얼마까지' 의도가 있으면 순서와 문장 길이에 상관없이 계산한다.
                asks_limit = bool(re.search(r"(?:한도|얼마까지)", q))
                has_calc_input = "account_value" in given or "payout_year" in given
                explicit_formula = bool(re.search(r"(?:연금\s*)?수령\s*한도.{0,20}계산|"
                                                  r"계산.{0,20}(?:연금\s*)?수령\s*한도", q))
                if not (explicit_formula or (asks_limit and has_calc_input)):
                    continue
            elif fid == "fm_hardship_withdrawal_limit":
                # 과거 smoke 계약: 의료비/휴직기간 같은 계산 입력을 실제로 제시한
                # 요양 중도인출 질문이면 '한도'라는 단어가 없더라도 공식 후보가 된다.
                # 사유만 묻는 일반 자격 질문에는 공식을 붙이지 않는다.
                asks_calc = bool(re.search(r"(?:중도\s*)?인출.{0,16}(?:한도|얼마)|"
                                           r"(?:한도|얼마까지).{0,16}(?:인출|빼|찾)", q))
                has_calc_input = "medical_cost" in given or "leave_months" in given
                if not (asks_calc or has_calc_input):
                    continue

            # applies_category만으로는 너무 넓다. '중도인출' 공식이라고 해서
            # 주택구입에도 상해질병 인출한도 공식을 적용하면 그대로 오답이다.
            appl = json.loads(f["applicable_conditions"] or "{}")
            bad = False
            for key, allowed in appl.items():
                v = given.get(key)
                if v is None:
                    bad = True           # 사유를 모르면 적용하지 않는다
                elif str(v) not in allowed:
                    bad = True
                if bad:
                    break
            if bad:
                continue
            out.append(dict(f))
        # 입력이 갖춰진 공식을 앞으로
        def ready(f):
            req = json.loads(f["required_inputs"])
            return all(k in given for k in req)
        out.sort(key=lambda f: 0 if ready(f) else 1)
        return out

    # -------------------------------------------------------- Evidence 변환
    def to_evidence(self, row, idx: int, cres=None, value_status=None,
                    status=None) -> dict[str, Any]:
        cond = f" ({row['condition_text']})" if row["condition_text"] else ""
        text = (f"{row['pension_type']} {row['category']} — "
                f"{row['item']}{cond}: {row['value_text']}")

        caveats = []
        if row["errata_note"]:
            caveats.append(f"원문 오류 정정: {row['errata_note']}")
        if row["pension_type_basis"]:
            caveats.append(f"제도 귀속 근거: {row['pension_type_basis']}")
        if row["valid_to"]:
            caveats.append(f"{row['valid_to']}까지 유효했던 규칙")
        elif row["time_scope"] == "historical":
            caveats.append("과거 제도 기준 — 현행 여부 확인 필요")
        if row["confidence"] == "low":
            caveats.append("원문 근거가 약함")
        conds = self.conditions_of(row["fact_id"])
        if any(c["is_inferred"] for c in conds):
            caveats.append("구간 경계 일부는 다음 구간에서 유도한 값 (원문 직접 표기 아님)")

        # 요건 미충족은 '근거 없음'이 아니라 **답 그 자체**다. 명시적으로 싣는다.
        unmet_labels = []
        for c in (cres.unmet if cres else []):
            v = c["condition_token"] if c["condition_token"] is not None \
                else (f"{c['condition_num']:g}" if c["condition_num"] is not None else "")
            unmet_labels.append(f"{c['condition_key']}{c['condition_op']}{v}")
        unknown_keys = list(cres.unknown_keys) if cres else []
        unknown_labels = [self.label_of(k) for k in unknown_keys]

        # 요건 판정 관련 주의는 **맨 앞**에 둔다. 생성기도 offline fallback도
        # 앞쪽 caveat만 인용하는 경로가 있어서, 뒤에 두면 조용히 사라진다.
        head = []
        if unmet_labels:
            head.append("질문 조건이 이 요건을 충족하지 않음: " + ", ".join(unmet_labels))
        if value_status == UNMET:
            head.append(f"질문에 주어진 값이 이 기준({row['value_op']} "
                        f"{row['value_num']:g})을 충족하지 않음")

        # ★ unknown은 '근거 없음'도 '충족'도 아니다. **되물어야 하는 상태**다.
        # 무엇을 모르는지 함께 실어야 생성 단계가 "○○를 알려주시면"이라고 쓸 수 있다.
        if status == UNKNOWN:
            head.append(
                "요건 충족 여부를 확정할 수 없음 — 다음 정보가 질문에 없음: "
                + ", ".join(unknown_labels or unknown_keys)
                + ". 충족한다고 단정하지 말 것.")
        elif status == MET and value_status == MET:
            head.append("질문에 주어진 값이 이 기준을 충족함")
        caveats = head + caveats

        return {
            "evidence_id": f"sql_{idx:03d}",
            "kind": "sql",
            "domain": "pension_rule",
            "text": text,
            "value": {
                "item": row["item"], "op": row["value_op"],
                "num": row["value_num"], "num_max": row["value_num_max"],
                "unit": row["value_unit"], "bool": row["value_bool"],
                "subject": row["value_subject"],
                "condition": row["condition_text"],
                "conditions": [{"key": c["condition_key"], "op": c["condition_op"],
                                "num": c["condition_num"], "max": c["condition_num_max"],
                                "unit": c["condition_unit"], "token": c["condition_token"],
                                "inferred": bool(c["is_inferred"])} for c in conds],
            },
            "score": 1.0,                     # SQL은 리랭킹 대상이 아니다
            "provenance": {
                "source_file": row["source_file"], "page": row["page"],
                "locator": row["source_locator"], "record_id": row["record_id"],
                "chunk_id": row["chunk_id"],
            },
            "quote": row["quote"],
            "confidence": row["confidence"],
            # met / unmet / unknown / None(미판정) — 4가지가 전부 다른 뜻이다.
            #   None    질문에 관련 정보가 하나도 없어 판정하지 않았다
            #   unknown 일부는 판정됐는데 나머지를 몰라 결론을 못 낸다 → 되물어야 한다
            "requirement_status": status,
            "unmet_conditions": unmet_labels,
            "unknown_conditions": unknown_keys,
            "unknown_condition_labels": unknown_labels,
            # calc 게이트와 후검증이 category·item으로 대조한다
            "category": row["category"],
            "item": row["item"],
            "pension_type": row["pension_type"],
            "fact_role": row["fact_role"],
            "valid_from": row["valid_from"], "valid_to": row["valid_to"],
            "time_scope": row["time_scope"],
            "rule_group_id": row["rule_group_id"],
            "caveats": caveats,
        }

    # -------------------------------------------------------------- 실행
    @staticmethod
    def _apply_quota(ranked, categories, limit):
        """여러 구분이 잡힌 질문에서 한 구분이 상한을 독식하지 않게 한다.

        v4는 마지막에 `kept[:limit]` 하나뿐이라, fact가 늘어나면 복합 질문에서
        앞 구분이 top-N을 대부분 먹고 다른 구분은 아예 빠졌다.
        구분별로 순서를 유지한 채 라운드로빈으로 뽑는다.
        """
        if len(categories) < 2 or len(ranked) <= limit:
            return ranked[:limit], False
        buckets = {}
        for item in ranked:
            buckets.setdefault(item[0]["category"], []).append(item)
        out, i = [], 0
        while len(out) < limit and any(buckets.values()):
            progressed = False
            for cat in list(buckets):
                if not buckets[cat] or len(out) >= limit:
                    continue
                out.append(buckets[cat].pop(0))
                progressed = True
            if not progressed:
                break
            i += 1
        # 라운드로빈은 순위를 흩뜨리므로 원래 순서로 되돌린다
        order = {id(x): n for n, x in enumerate(ranked)}
        out.sort(key=lambda x: order[id(x)])
        return out, True

    def run(self, question: str, limit: int = 12) -> dict[str, Any]:
        pts = detect_pension_types(question)
        cats = detect_categories(question)
        given = detect_conditions(question)

        # 구분을 못 잡았으면 조건 키로 유추해본다. 그래도 없으면 SQL을 돌리지 않는다.
        inferred = []
        if not cats:
            for k in given:
                inferred += CONDITION_CATEGORY_HINT.get(k, [])
            cats = sorted(set(inferred))

        trace = [f"sql/pension_rule: 제도={pts or '(미지정)'} 구분={cats or '(미지정)'}"
                 + (" (조건 키로 유추)" if inferred else "")]
        if not cats:
            trace.append("sql/pension_rule: 구분 미탐지 → DB 전체 조회를 하지 않음 "
                         "(무관한 수치가 [확정 수치] 블록에 섞이는 것을 막는다)")
            return {"evidence": [], "formulas": [], "think_trace": trace,
                    "detected": {"pension_types": pts, "categories": [],
                                 "conditions": given}}

        intents = detect_item_intents(question)
        if intents:
            trace.append(f"sql/intent: 질문이 직접 지목한 항목 {intents} → 정렬 1순위")

        rows = self.lookup(pts, cats)                     # 전체 조회
        ranked, dropped = self.rank(rows, given, question)  # 조건 필터 + 정렬
        ranked, dup = self._dedupe(ranked, pts)           # 동일 규칙 중복만 정리
        shown, quota = self._apply_quota(ranked, cats, limit)

        trace.append(f"sql/pension_rule: 관련 {len(rows)}행 → selector 배제 {len(dropped)}행 "
                     f"→ 중복 정리 {len(dup)}행 → 상위 {len(shown)}행 (HCX 호출 0회)")
        if quota:
            trace.append(f"sql/pension_rule: 구분 {len(cats)}개 라운드로빈 적용 "
                         f"(한 구분이 상한을 독식하지 않게)")
        if len(rows) >= HARD_CAP:
            trace.append(f"sql/pension_rule: ⚠ 조회 상한 {HARD_CAP} 도달 — "
                         f"구분을 좁히거나 상한을 올려야 함")
        if given:
            trace.append(f"sql/condition: 질의에서 추출한 조건 {given}")

        n_unmet = sum(1 for _, _, _, s in shown if s == UNMET)
        n_unknown = sum(1 for _, _, _, s in shown if s == UNKNOWN)
        if n_unmet:
            trace.append(f"sql/requirement: unmet {n_unmet}건 "
                         f"(제외하지 않고 '충족하지 않음' 근거로 제시)")
        if n_unknown:
            miss = []
            for _, c, _, s in shown:
                if s == UNKNOWN:
                    miss += [k for k in c.unknown_keys if k not in miss]
            trace.append(f"sql/requirement: unknown {n_unknown}건 — 부족한 정보 {miss} "
                         f"(충족으로 단정하지 않고 되묻는다)")

        evidence = [self.to_evidence(r, i, c, v, s)
                    for i, (r, c, v, s) in enumerate(shown, 1)]
        formulas = self.formulas_for(pts, cats, given, question)
        return {"evidence": evidence, "formulas": formulas, "think_trace": trace,
                "detected": {"pension_types": pts, "categories": cats,
                             "conditions": given, "item_intents": intents}}


# ------------------------------------------------------------------ calc
# 질의어 키 → 공식 변수명
_VAR_ALIAS = {"year_n": "payout_year"}

# calc 결과 상태. 숫자를 냈는지 못 냈는지, 못 냈으면 **왜** 못 냈는지를 구분한다.
CALC_OK = "computed"
CALC_UNMET = "blocked_unmet"        # 요건 확정 미충족 → 그 금액은 존재하지 않는다
CALC_UNKNOWN = "blocked_unknown"    # 요건 판정 불가 → 무엇을 알려달라고 되묻는다
CALC_NO_INPUT = "insufficient_input"
CALC_NONE = "no_formula"


class CalcResult:
    __slots__ = ("status", "evidence", "formula", "reason", "missing",
                 "missing_labels", "blockers")

    def __init__(self, status, evidence=None, formula=None, reason="",
                 missing=None, blockers=None, missing_labels=None):
        self.status = status
        self.evidence = evidence          # CALC_OK일 때만 채워진다
        self.formula = formula
        self.reason = reason
        self.missing = missing or []      # 되묻기용 조건 키 (기계용)
        # 사람에게 보여줄 문구. 'care_months를 알려주세요'라고 쓰면 안 된다.
        self.missing_labels = missing_labels or []
        self.blockers = blockers or []    # 미충족을 만든 요건 설명

    def ask_for(self) -> list[str]:
        return self.missing_labels or self.missing

    def blocked(self) -> bool:
        # 요건 미충족/미확정뿐 아니라 필수 계산 입력이 빠진 경우도 숫자를 산출하면 안 된다.
        # 이를 block으로 보아 answer_guard가 근거 없는 계산값을 막도록 한다.
        return self.status in (CALC_UNMET, CALC_UNKNOWN, CALC_NO_INPUT)


def _precondition_applies(pc: dict, given: dict) -> bool:
    """`when`이 있으면 그 상황일 때만 이 요건을 본다.

    '6개월 이상 요양'은 요양 사유일 때의 요건이다. 재난피해 인출에까지
    요양기간을 요구하면 멀쩡한 계산이 막힌다.
    """
    when = pc.get("when") or {}
    for key, allowed in when.items():
        v = given.get(key)
        if v is None or str(v) not in allowed:
            return False
    return True


def check_preconditions(formula: dict, given: dict) -> tuple[str, list, list, list]:
    """계산 **전에** 확정돼야 하는 요건을 3치로 판정한다.

    반환: (met|unmet|unknown, 미충족 설명 목록, 부족한 조건 키, 부족한 조건 라벨)

    라벨을 함께 돌려주는 이유: 답변에 'care_months를 알려주세요'라고 쓰면 안 된다.
    사용자가 읽는 문구는 원문에서 뽑은 label('6개월 이상 요양…')이어야 한다.

    v3은 guard가 '입력값이 다 있는가'만 봤다. 그래서
    '요양 1개월 + 의료비 500만원 + 휴직 3개월'에 11,500,000원을 확정 수치로 냈다.
    6개월 요건을 못 채웠으므로 그 금액은 애초에 존재하지 않는 값이다.
    """
    raw = formula.get("preconditions") or "[]"
    pcs = json.loads(raw) if isinstance(raw, str) else raw

    verdicts, blockers, missing, labels = [], [], [], []

    def note_missing(key, pc):
        if key not in missing:
            missing.append(key)
            labels.append(pc.get("label") or key)

    for pc in pcs:
        if not _precondition_applies(pc, given):
            continue
        key = pc["key"]
        if key not in given:
            verdicts.append(UNKNOWN)
            note_missing(key, pc)
            continue
        cond = {"condition_key": key, "condition_op": pc["op"],
                "condition_num": pc.get("num"), "condition_num_max": pc.get("max"),
                "condition_token": pc.get("token")}
        v = tri.from_bool(condition_holds(cond, given))
        verdicts.append(v)
        if v == UNMET:
            blockers.append(pc.get("label") or key)
        elif v == UNKNOWN:
            note_missing(key, pc)
    return tri.and_(verdicts), blockers, missing, labels


def run_formula(formula: dict, given: dict, facts: list | None = None) -> CalcResult:
    """공식을 돌리되, **요건이 확정되기 전에는 숫자를 만들지 않는다.**

    막는 근거는 두 가지다.
      1. 공식 자신의 preconditions  (원문에서 뽑은 명시적 요건)
      2. 같은 category에서 이미 **unmet으로 확정된** fact
         '54세인데 평가액 1억 3년차 한도?' → 개시연령 55세 미충족이 확정이므로
         한도를 계산해 봐야 받을 수 없는 금액이다.
         unknown은 여기서 막지 않는다 — 그러면 조건이 붙은 거의 모든 계산이
         멈춰서 아무도 안 쓰게 된다. 확정 미충족만 막는다.
    """
    status, blockers, missing, labels = check_preconditions(formula, given)
    if status == UNMET:
        return CalcResult(CALC_UNMET, formula=formula, blockers=blockers,
                          reason="요건 미충족: " + ", ".join(blockers))
    if status == UNKNOWN:
        return CalcResult(CALC_UNKNOWN, formula=formula, missing=missing,
                          missing_labels=labels,
                          reason="요건 판정 불가 (정보 부족): " + ", ".join(missing))

    cat = formula.get("applies_category")
    hard = [f for f in (facts or [])
            if f.get("kind") == "sql" and f.get("category") == cat
            and f.get("requirement_status") == UNMET]
    if hard:
        labels = [f"{f['item']}({f.get('value',{}).get('condition') or ''})".strip()
                  for f in hard[:3]]
        return CalcResult(CALC_UNMET, formula=formula, blockers=labels,
                          reason="같은 구분에서 요건 미충족이 확정됨: " + ", ".join(labels))

    ev = eval_formula(formula, given)
    if ev is None:
        req = formula.get("required_inputs") or "[]"
        req = json.loads(req) if isinstance(req, str) else req
        miss = [k for k in req if k not in given]

        # 기계 키(payout_year)를 사용자에게 그대로 노출하지 않도록 공식 변수 설명에서
        # 사람이 읽을 라벨을 만든다. _VAR_ALIAS는 formula 변수명 -> 질의 조건 키다.
        raw_vars = formula.get("variables") or "[]"
        raw_vars = json.loads(raw_vars) if isinstance(raw_vars, str) else raw_vars
        desc_by_name = {v.get("name"): v.get("desc") or v.get("name")
                        for v in raw_vars if isinstance(v, dict)}
        labels = []
        for key in miss:
            var_name = next((var for var, src in _VAR_ALIAS.items() if src == key), key)
            desc = str(desc_by_name.get(var_name) or key)
            # 괄호 뒤의 긴 설명은 근거/공식 블록에 이미 있으므로 되묻기에는 핵심 명칭만 쓴다.
            labels.append(desc.split("(", 1)[0].strip() or desc)
        return CalcResult(CALC_NO_INPUT, formula=formula, missing=miss,
                          missing_labels=labels,
                          reason=f"필요한 입력 부족 {miss}")
    return CalcResult(CALC_OK, evidence=ev, formula=formula)


def eval_formula(formula: dict, values: dict) -> dict[str, Any] | None:
    """확정 공식은 코드가 계산한다. LLM에게 산수를 시키지 않는다.

    팀 실측: HCX가 평가액/(11-연차)×120% 를 ×(11-연차)로 잘못 적용해
    8배 틀린 값을 두 라운드 연속 냈다.

    guard를 충족하지 못하면 None을 돌려주고, 답변은 공식만 제시한다.
    계산 안 하는 것보다 엉뚱한 금액을 자신 있게 말하는 게 훨씬 위험하다.
    """
    variables = formula["variables"]
    if isinstance(variables, str):
        variables = json.loads(variables)
    # safe_eval에 넘길 변수는 **수치만**이다. 부정 필터(not isinstance(v, str))는
    # 집합·리스트 같은 값을 그대로 통과시켜 계산 중에 TypeError를 낸다.
    vals = {k: v for k, v in values.items() if isinstance(v, (int, float))}
    for var, src in _VAR_ALIAS.items():
        if var not in vals and src in vals:
            vals[var] = vals[src]
    needed = [v["name"] for v in variables]
    if any(n not in vals for n in needed):
        return None
    if formula["formula_id"].startswith("fm_pension_limit"):
        yn = vals.get("year_n")
        if yn is None or yn >= 11 or yn < 1:
            return None                   # 한도는 10년차까지만. 분모가 0 이하가 되면 안 됨
    try:
        result = safe_eval(formula["expression"], vals)
    except (UnsafeExpression, ZeroDivisionError, TypeError, OverflowError):
        return None
    if result != result or result in (float("inf"), float("-inf")) or result < 0:
        return None
    return {
        "evidence_id": f"calc_{formula['formula_id']}",
        "kind": "calc", "domain": "pension_rule",
        "text": f"{formula['name']} = {result:,.0f}",
        "value": {"item": formula["name"], "num": result, "unit": None,
                  "expression": formula["expression"],
                  "inputs": {k: vals[k] for k in needed}},
        "score": 1.0,
        "provenance": {"source_file": formula["source_file"],
                       "locator": formula["source_locator"], "page": None,
                       "record_id": formula["record_id"],
                       "chunk_id": formula["chunk_id"]},
        "quote": formula["quote"], "confidence": "high", "caveats": [],
    }


if __name__ == "__main__":
    agent = PensionSQLAgent()
    qs = sys.argv[1:] or [
        "IRP 세액공제 한도가 얼마야?",
        "총급여가 5,000만원인데 IRP 세액공제율 얼마야?",
        "IRP 연금수령 21년차인데 세율이 어떻게 돼?",
        "연금저축 72세에 연금 받으면 세율이 몇 퍼센트야?",
        "개인연금저축은 중도인출 되나요?",
        "요양으로 중도인출하는데 의료비 500만원 휴직 3개월이면 한도가 얼마야?",
    ]
    for q in qs:
        print("=" * 78); print("Q:", q)
        out = agent.run(q, limit=5)
        for t in out["think_trace"]:
            print("   ", t)
        for e in out["evidence"]:
            p = e["provenance"]
            print(f"    • {e['text']}")
            print(f"      근거: {p['source_file']} {p['locator']} ({p['record_id']})")
        for fm in out["formulas"][:1]:
            res = run_formula(fm, out["detected"]["conditions"], out["evidence"])
            print(f"    [공식] {fm['name']} → "
                  + (res.evidence["text"] if res.status == CALC_OK
                     else f"{res.status} — {res.reason}"))
