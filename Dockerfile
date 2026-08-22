# 공모전 제출 요건 1번 — "재현 가능한 개발 환경 정의"용 Dockerfile.
#
# data/ 와 .env 는 이미지에 굽지 않는다.
#   - data/ 는 .gitignore 대상이라 저장소에도 없다. pension_rules.db,
#     junil_rag_embeddings_v3.json, fund_prospectus_v2.sqlite 는 팀원에게
#     직접 받아 로컬 data/ 폴더에 두고, 컨테이너 실행 시 볼륨으로 마운트한다.
#   - .env(CLOVASTUDIO_API_KEY)는 시크릿이라 이미지에 포함하지 않는다.
#     .dockerignore 에도 명시적으로 제외해 뒀다.
#
# 빌드:
#   docker build -t pension-agent .
#
# 실행 (프로젝트 루트에 data/ 와 .env 가 이미 준비돼 있어야 함):
#   docker run -p 8000:8000 \
#     -v "$(pwd)/data:/app/data:ro" \
#     -v "$(pwd)/.env:/app/.env:ro" \
#     pension-agent
#
# 확인:
#   curl http://localhost:8000/health

FROM python:3.11-slim

WORKDIR /app

# pdfplumber(Pillow 등 C 확장 의존) 빌드 대비 최소 시스템 패키지.
# 평가 서버에서는 build_*.py(데이터 재구축용) 를 돌릴 일이 없지만,
# requirements.txt 설치 자체가 이 패키지들 없이 실패하는 환경이 있어
# 안전하게 넣어두고 설치 후 apt 캐시는 지운다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ 는 볼륨 마운트로 채워지므로 빈 폴더만 미리 만들어 둔다
# (마운트 전에 컨테이너가 먼저 뜨는 상황에서도 os.path 관련 예외를 피한다).
RUN mkdir -p data/vector_db

EXPOSE 8000

# 평가 API는 컨테이너 밖(0.0.0.0)에서 들어오는 요청을 받아야 하므로
# main.py 문서의 127.0.0.1 예시와 달리 0.0.0.0으로 바인딩한다.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
