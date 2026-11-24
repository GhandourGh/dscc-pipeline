# Build context = repository root:  docker build -f api/Dockerfile -t ml-api .
# ---- Stage 1: build wheels --------------------------------------------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY api/requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements.txt \
 && /opt/venv/bin/pip uninstall -y pip setuptools \
 && find /opt/venv \( -name tests -o -name __pycache__ \) -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name "*.pyi" -delete

# ---- Stage 2: runtime ---------------------------------------------------------
FROM python:3.12-slim AS runtime
ARG API_VERSION=1.0.0
ARG GIT_SHA=local
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_VERSION=${API_VERSION} \
    GIT_SHA=${GIT_SHA} \
    MODEL_PATH=/app/model/model-1.0.0.joblib \
    PORT=8000

RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY api/app ./app
COPY model/model-*.joblib model/model-*.json ./model/
USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health',timeout=2)" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
