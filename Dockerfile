FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app
# Hash-pinned dependencies exported from uv.lock:
#   uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.lock
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock \
    && useradd --system --uid 10001 --no-create-home gateway
COPY gateway ./gateway

USER gateway
EXPOSE 8080

# /health probes both upstreams (cached 15s); allow for slow upstreams.
HEALTHCHECK --interval=60s --timeout=100s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/health', timeout=95)"

CMD ["python", "-m", "gateway"]
