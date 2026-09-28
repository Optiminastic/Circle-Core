# circle-be — production image (FastAPI + uvicorn).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is used by the container HEALTHCHECK. psycopg[binary] ships its own libpq
# wheel, so no build toolchain is needed.
# tesseract-ocr reads uploaded joining documents (see app/services/ocr.py). It's
# a system binary, not a pip package; without it the app still runs and the
# feature reports `ocr_not_configured`. Scanned PDFs are rasterised in-process
# by pypdfium2, so no poppler is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Tesseract's OpenMP threading hurts throughput inside a multi-worker container
# and would otherwise spread across every core of a shared host.
ENV OMP_THREAD_LIMIT=1

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code.
COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

# Two workers; NEVER use --reload in production.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
