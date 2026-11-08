# DriveMesh container: stateless app (metadata lives in Postgres, data on linked accounts).
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY drivemesh ./drivemesh
RUN pip wheel --wheel-dir /wheels ".[pg]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DRIVEMESH_ENV=prod DRIVEMESH_LOG_JSON=true PORT=8000
RUN useradd --create-home --uid 10001 drivemesh
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels drivemesh[pg] && rm -rf /wheels
USER drivemesh
WORKDIR /home/drivemesh
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=4)"
# Migrations run at startup (idempotent); workers run in-process (ADR-011).
CMD ["sh", "-c", "exec drivemesh serve --host 0.0.0.0 --port ${PORT}"]
