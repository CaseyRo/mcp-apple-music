FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# ca-certificates: httpx uses the OS trust store
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv==0.13.5 && \
    uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
    pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && \
    pip install --no-cache-dir --no-deps . && \
    rm /tmp/requirements.txt && \
    addgroup --system mcp && adduser --system --ingroup mcp mcp && \
    mkdir -p /data /config && chown mcp:mcp /data

USER mcp

# Release version (the git tag); /health reports it. Unset → pyproject version.
ARG APP_VERSION=""
ENV APP_VERSION=$APP_VERSION

ENV TRANSPORT=http
ENV HOST=0.0.0.0
ENV PORT=8000
# All state (tokens, mirror windows, playlist IDs) lives here: mount a volume.
ENV DATA_DIR=/data

EXPOSE 8000

# Alive is enough: "degraded" (say, waiting for sign-in) is a running server.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request,json,sys; r=urllib.request.urlopen('http://localhost:8000/health',timeout=3); sys.exit(0 if json.loads(r.read()).get('status') in ('healthy','degraded') else 1)"

CMD ["mcp-apple-music"]
