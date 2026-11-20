FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache \
    SCHULKASSE_DB=/data/schulkasse.sqlite3 SCHULKASSE_UPLOAD_DIR=/data/belege SCHULKASSE_BACKUP_DIR=/data/backups \
    SCHULKASSE_BRIEF_CONFIG=/data/brief.toml SCHULKASSE_AUTO_MIGRATE=0 SCHULKASSE_HINTER_PROXY=0
WORKDIR /app
# WeasyPrint: Pango/HarfBuzz; Schriften der PDF-Vorlagen (DejaVu Sans, Liberation Sans)
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY schulkasse ./schulkasse
COPY gunicorn.conf.py brief.example.toml ./
RUN groupadd --gid 10001 schulkasse && useradd --uid 10001 --gid 10001 --no-create-home schulkasse \
    && mkdir -p /data/belege && chown -R 10001:10001 /data && chmod 700 /data /data/belege
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "--config", "gunicorn.conf.py"]

FROM runtime AS tests
# Die vorhandenen Tests prüfen den nativen Ablauf (Migration beim Start); test_platform.py schaltet sie ab.
ENV SCHULKASSE_AUTO_MIGRATE=1
USER 0
COPY requirements-test.lock pyproject.toml ./
RUN pip install --no-cache-dir --require-hashes -r requirements-test.lock
COPY tests ./tests
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM runtime AS final
