FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/relay
COPY requirements-runtime-lock.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-runtime-lock.txt && useradd --create-home --uid 10001 relay
COPY --chown=relay:relay app ./app
COPY --chown=relay:relay migrations ./migrations
COPY --chown=relay:relay alembic.ini ./
COPY --chown=relay:relay scripts ./scripts
USER relay
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "172.30.0.2", "--no-access-log", "--limit-concurrency", "200", "--timeout-keep-alive", "5"]
