FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

# Application is pure standard library; install nothing at runtime.
COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=12 \
    CMD python -c "import json,os,sys,urllib.request; \
port=os.environ.get('PORT','8080'); \
r=urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2); \
sys.exit(0 if r.status==200 and json.loads(r.read()).get('status')=='ok' else 1)" \
    || exit 1

CMD ["python", "-m", "app.server"]
