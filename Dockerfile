FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY warehouse ./warehouse
COPY wsgi.py entrypoint.py .

RUN useradd --system --uid 10001 --home /app appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
# The entrypoint starts as root only to fix named-volume ownership, then
# drops to appuser before gunicorn runs.
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "wsgi:app"]
