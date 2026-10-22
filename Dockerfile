FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXPORT_DIR=/tmp/catalogue-exports \
    SYNC_LEDGER_BACKEND=dynamodb \
    STORAGE_BACKEND=aws \
    LOG_FORMAT=json

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser
COPY app ./app
USER 10001:10001
# Exit codes: 0 succeeded, 1 failed (re-run resumes), 2 needs reconciliation.
ENTRYPOINT ["python", "-m", "app.main"]
