# Build context: backend/

FROM python:3.11-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential default-libmysqlclient-dev pkg-config \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements/ requirements/
ARG REQUIREMENTS=prod
# Generous timeout/retries: slow or flaky connections to PyPI should not fail the build. The pip cache
# survives between builds (BuildKit cache mount), so a failed or changed build doesn't download everything
# again: a corrupted download on a slow link once cost an hour (2026-11-26).
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_NO_CACHE_DIR= pip wheel --timeout 120 --retries 10 --wheel-dir /wheels -r requirements/${REQUIREMENTS}.txt


FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmariadb3 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system app && useradd --system --gid app --home /app app \
 && mkdir -p /data/private/reports /app/staticfiles && chown -R app:app /data /app
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels
WORKDIR /app
COPY --chown=app:app . .
USER app
ARG REQUIREMENTS=prod
# Production image: bake hashed, compressed static files (served by WhiteNoise). The env values below are
# throwaway build-time placeholders so settings can load; real secrets are only supplied at runtime.
RUN if [ "$REQUIREMENTS" = "prod" ]; then \
      DJANGO_SETTINGS_MODULE=config.settings.prod \
      DJANGO_SECRET_KEY=build-only \
      DJANGO_ALLOWED_HOSTS=build.invalid \
      REPORT_PRICE_PAISE=1 \
      DATABASE_URL=sqlite:////tmp/build.sqlite3 \
      FIELD_ENCRYPTION_KEYS=YnVpbGQtb25seS1rZXktMDAwMDAwMDAwMDAwMDAwMDA= \
      BLIND_INDEX_KEY=build-only-blind-index-key-000000000 \
      REPORT_FILE_ENCRYPTION_KEYS=b:YnVpbGQtb25seS1maWxlLWtleS0wMDAwMDAwMDAwMDA= \
      python manage.py collectstatic --noinput -v 0 && rm -f /tmp/build.sqlite3; \
    fi
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=3).status == 200 else 1)"
CMD ["gunicorn", "-c", "gunicorn.conf.py", "config.wsgi:application"]
