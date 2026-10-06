# circle voice bridge - Vapi custom transcriber/voice backed by Sarvam.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /bridge

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bridge ./bridge

# Never run as root.
RUN useradd --system --no-create-home bridge
USER bridge

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=3)" || exit 1

# One worker: each call holds a live WebSocket, and the bridge keeps no shared state.
CMD ["uvicorn", "bridge.main:app", "--host", "0.0.0.0", "--port", "8080"]
