import json
import os
import uuid
import re
from pathlib import Path
import numpy as np
import requests
from dotenv import load_dotenv

# ============================================================
# PATH 설정
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
EMBEDDING_FILE = BASE_DIR / "data" / "vector_db" / "rag_embeddings_v2.json"

load_dotenv(BASE_DIR / ".env")
API_KEY = os.getenv("CLOVASTUDIO_API_KEY")

if not API_KEY:
    raise RuntimeError(".env에서 CLOVASTUDIO_API_KEY를 찾을 수 없습니다.")

# ============================================================
# CLOVA API 설정
# ============================================================
EMBEDDING_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
RERANKER_URL = "https://clovastudio.stream.ntruss.com/v1/api-tools/reranker"
HCX_URL = "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-007"

TIMEOUT = 90
HYBRID_TOP_K = 10

PENSION_TYPE_BONUS = 0.08
PRIMARY_TOPIC_BONUS = 0.06
ASPECT_BONUS = 0.05
KEYWORD_BONUS = 0.03
SPECIAL_TYPE_PENALTY = 0.08

SPECIAL_PENSION_TYPES = {"과학기술인연금", "ISA_연계", "개인연금저축_구"}

def build_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

def detect_query_metadata(query):
    q = query.replace(" ", "").lower()
    pension_types = set()
    aspects = set()
    primary_topics = set()
    keywords = set()

    if "irp" in q or "개인형퇴직연금" in q:
        pension_types.add("IRP")
    if "dc" in q or "확정기여형" in q:
        pension_types.add("DC")
    if "db" in q or "확정급여형" in q:
        pension_types.add("DB")
    if "연금저축" in q:
        pension_types.add("연금저축계좌")
    if "과학기술인" in q or "과기공" in q or "공제회연금" in q:
        pension_types.add("과학기술인연금")
    if "isa" in q:
        pension_types.add("ISA_연계")

    if "중도인출" in q or "중간인출" in q:
        aspects.add("중도인출")
        primary_topics.add("중도인출·해지")
        keywords.add("중도인출")
    if "해지" in q:
        aspects.add("해지")
        primary_topics.add("중도인출·해지")
        keywords.add("해지")
    if "세액공제" in q or "공제한도" in q:
        aspects.add("세액공제")
        primary_topics.add("세금·세액공제·재원확정")
        keywords.add("세액공제")
    if "납입한도" in q:
        aspects.add("납입한도")
        primary_topics.add("부담금·납입")
        keywords.add("납입한도")
    if "위험자산" in q or "위험투자" in q:
        aspects.add("위험자산")
        primary_topics.add("운용·매매")
        keywords.add("위험자산")
    if "투자한도" in q or "투자비율" in q or "몇%" in q or "몇프로" in q:
        aspects.add("투자한도")
        primary_topics.add("운용·매매")
        keywords.add("투자한도")
    if "매수" in q or "매도" in q or "리밸런싱" in q:
        aspects.add("상품매매")
        primary_topics.add("운용·매매")
    if "이전" in q or "이관" in q or "옮길" in q or "옮기" in q or "금융회사변경" in q:
        aspects.add("계약이전")
        primary_topics.add("이전·전환·승계")
        keywords.add("이전")
    if "계좌이체" in q:
        aspects.add("계좌이체")
        primary_topics.add("이전·전환·승계")
    if "연금수령" in q or "연금받" in q:
        aspects.add("연금수령")
        primary_topics.add("연금개시·수령")
    if "연금개시" in q or "개시조건" in q:
        aspects.add("연금개시")
        primary_topics.add("연금개시·수령")
    if "부담금" in q:
        aspects.add("부담금")
        primary_topics.add("부담금·납입")
    if "압류" in q:
        aspects.add("압류")
        primary_topics.add("권리보호·담보대출·압류")
    if "담보대출" in q:
        aspects.add("담보대출")
        primary_topics.add("권리보호·담보대출·압류")
    if "디폴트옵션" in q:
        aspects.add("디폴트옵션")
        primary_topics.add("디폴트옵션")
        keywords.add("디폴트옵션")

    return {
        "pension_types": pension_types,
        "aspects": aspects,
        "primary_topics": primary_topics,
        "keywords": keywords,
    }

def embed_query(text):
    response = requests.post(
        EMBEDDING_URL,
        headers=build_headers(),
        json={"text": text},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Embedding 오류 {response.status_code}: {response.text[:500]}")
    
    embedding = response.json().get("result", {}).get("embedding")
    if not embedding:
        raise RuntimeError("Query embedding이 없습니다.")
    return np.array(embedding, dtype=np.float32)

def cosine_similarity(q, d):
    q_norm = np.linalg.norm(q)
    d_norm = np.linalg.norm(d)
    if q_norm == 0 or d_norm == 0:
        return 0.0
    return float(np.dot(q, d) / (q_norm * d_norm))

def calculate_metadata_bonus(query_meta, record):
    bonus = 0.0
    pension_types = set(record.get("pension_types") or [])
    aspects = set(record.get("knowledge_aspects") or [])
    topic = record.get("primary_topic")

    if query_meta["pension_types"] & pension_types:
        bonus += PENSION_TYPE_BONUS
    if topic in query_meta["primary_topics"]:
        bonus += PRIMARY_TOPIC_BONUS

    matched_aspects = query_meta["aspects"] & aspects
    bonus += len(matched_aspects) * ASPECT_BONUS

    text = str(record.get("text") or "") + " " + str(record.get("embedding_text") or "")
    normalized_text = text.replace(" ", "").replace("\n", "").lower()

    for keyword in query_meta["keywords"]:
        normalized_keyword = keyword.replace(" ", "").lower()
        if normalized_keyword in normalized_text:
            bonus += KEYWORD_BONUS

    if not query_meta["pension_types"]:
        if pension_types & SPECIAL_PENSION_TYPES:
            bonus -= SPECIAL_TYPE_PENALTY

    return bonus

def hybrid_search(query, records, top_k=HYBRID_TOP_K):
    q_vector = embed_query(query)
    query_meta = detect_query_metadata(query)
    results = []

    for record in records:
        d_vector = np.array(record["embedding"], dtype=np.float32)
        vector_score = cosine_similarity(q_vector, d_vector)
        bonus = calculate_metadata_bonus(query_meta, record)
        results.append({
            "record": record,
            "vector_score": vector_score,
            "metadata_bonus": bonus,
            "hybrid_score": vector_score + bonus,
        })

    results.sort(key=lambda x: x["hybrid_score"], reverse=True)
    return results[:top_k]

def rerank(query, results):
    documents = []
    for item in results:
        record = item["record"]
        text = (record.get("text") or record.get("embedding_text") or "").strip()
        if not text:
            continue
        documents.append({"id": record["chunk_id"], "doc": text})

    response = requests.post(
        RERANKER_URL,
        headers=build_headers(),
        json={"query": query, "documents": documents, "maxTokens": 500},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Reranker 오류 {response.status_code}: {response.text[:1000]}")
    return response.json().get("result") or {}

def build_context(reranker_result):
    cited = reranker_result.get("citedDocuments") or []
    contexts = []
    evidence_map = {}
    evidence_index = 1

    for doc in cited:
        chunk_id = doc.get("id")
        text = (doc.get("doc") or "").strip()
        if not chunk_id or not text:
            continue
        contexts.append(f"[근거 {evidence_index}]\n{text}")
        evidence_map[evidence_index] = chunk_id
        evidence_index += 1

    return "\n\n".join(contexts), evidence_map

def build_source_map(records):
    source_map = {}
    for record in records:
        chunk_id = record.get("chunk_id")
        if not chunk_id:
            continue
        source_map[chunk_id] = {
            "source_file": record.get("source_file"),
            "page_start": record.get("page_start"),
            "page_end": record.get("page_end"),
            "major_title": record.get("major_title"),
            "sub_title": record.get("sub_title"),
        }
    return source_map

def get_used_sources(used_evidence, evidence_map, source_map):
    sources = []
    seen = set()
    for evidence_number in used_evidence:
        chunk_id = evidence_map.get(evidence_number)
        if not chunk_id:
            continue
        info = source_map.get(chunk_id)
        if not info:
            continue
        key = (info.get("source_file"), info.get("page_start"), info.get("page_end"))
        if key in seen:
            continue
        seen.add(key)
        sources.append(info)
    return sources

def generate_answer(query, context):
    system_prompt = """너는 연금·퇴직연금 전문 AI 상담 에이전트다.
제공된 [검색 근거]만 사용하여 질문에 답변한다.

[작성 원칙]
1. 사용자에게 추가 질문(역질문)을 절대 하지 않는다. 질문이 포괄적이거나 조건이 부족한 경우 가능한 모든 경우의 수를 조건별로 나누어 한 번에 완결되게 설명한다.
2. 금액, 세율, 기간, 연령, 한도 등 수치는 근거에 명시된 값을 변형 없이 정확하게 제시한다.
3. 근거에 없는 사실은 추측하지 않는다.
4. 반드시 아래 JSON 포맷으로만 응답한다:
{
  "answer": "사용자에게 보여줄 최종 답변",
  "used_evidence": [1, 2]
}"""

    user_prompt = f"[사용자 질문]\n{query}\n\n[검색 근거]\n{context}\n\n검색 근거만 사용하여 역질문 없이 완결된 답변을 작성하라. 반드시 JSON만 출력하라."

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "topP": 0.8,
        "temperature": 0.1,
        "maxCompletionTokens": 1200,
    }

    response = requests.post(HCX_URL, headers=build_headers(), json=payload, timeout=TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"HCX 오류 {response.status_code}: {response.text[:1000]}")

    message = response.json().get("result", {}).get("message", {}).get("content", "").strip()
    if message.startswith("```"):
        lines = message.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        message = "\n".join(lines).strip()

    # 제어 문자 및 strict=False 처리로 JSONDecodeError 방지
    try:
        parsed = json.loads(message, strict=False)
    except json.JSONDecodeError:
        try:
            cleaned_message = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', message)
            parsed = json.loads(cleaned_message, strict=False)
        except Exception:
            parsed = {
                "answer": message,
                "used_evidence": []
            }

    cleaned_evidence = [int(v) for v in parsed.get("used_evidence", []) if str(v).isdigit() and int(v) > 0]
    return {
        "answer": parsed.get("answer", "").strip(),
        "used_evidence": cleaned_evidence,
    }

class PensionRAGAgent:
    def __init__(self, embedding_file=EMBEDDING_FILE):
        with open(embedding_file, "r", encoding="utf-8") as f:
            self.records = json.load(f)
        self.source_map = build_source_map(self.records)

    def ask(self, query):
        hybrid_results = hybrid_search(query, self.records, top_k=HYBRID_TOP_K)
        reranker_result = rerank(query, hybrid_results)
        context, evidence_map = build_context(reranker_result)

        if not context:
            return {
                "query": query,
                "answer": "제공된 자료에서 관련 근거를 확인하기 어렵습니다.",
                "sources": [],
                "used_evidence": [],
                "raw_context": ""
            }

        generation = generate_answer(query, context)
        sources = get_used_sources(generation.get("used_evidence", []), evidence_map, self.source_map)

        return {
            "query": query,
            "answer": generation.get("answer", ""),
            "sources": sources,
            "used_evidence": generation.get("used_evidence", []),
            "raw_context": context
        }