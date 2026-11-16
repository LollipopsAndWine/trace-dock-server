FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8080

WORKDIR /app

COPY pyproject.toml README.md main.py ./
COPY trace_dock_server ./trace_dock_server

RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 tracedock \
    && useradd --uid 10001 --gid tracedock --create-home --shell /usr/sbin/nologin tracedock

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"

CMD ["python", "-m", "trace_dock_server"]
