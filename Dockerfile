FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY gateway ./gateway
RUN pip install . && useradd --system --uid 10001 --no-create-home gateway

USER gateway
EXPOSE 8080

# /health probes both upstreams (cached 15s); allow for slow upstreams.
HEALTHCHECK --interval=60s --timeout=90s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/health', timeout=85)"

CMD ["sc-gateway"]
