# ghbot MCP 서버 Cloud Run 이미지. 빌드·배포는 .github/workflows/deploy.yml이 한다.
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./

# Cloud Run이 PORT(기본 8080)를 넣어주고 server.py가 그대로 쓴다.
ENV MCP_TRANSPORT=streamable-http
CMD ["python", "server.py"]
