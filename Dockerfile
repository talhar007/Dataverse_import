# Excel -> Dataverse importer
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (only what the service needs)
COPY metaforge_dataverse ./metaforge_dataverse
COPY run_api.py .

# Run as a non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Token/server are read from the environment at runtime (docker --env-file / compose env_file):
#   DATAVERSE_TOKEN=...   DATAVERSE_URL=https://134.95.195.250   DATAVERSE_INSECURE=true
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "metaforge_dataverse.app:app", "--host", "0.0.0.0", "--port", "8000"]
