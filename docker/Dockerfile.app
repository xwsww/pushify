FROM python:3.13-slim

ARG APP_UID=1000
ARG APP_GID=1000

# Copy Docker CLI so the update helper can drive host compose.
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

# Create non-root user
RUN addgroup --gid "${APP_GID}" appgroup \
    && adduser --uid "${APP_UID}" --gid "${APP_GID}" --system --home /app appuser

# System dependencies
RUN apt-get update && apt-get install -y bash curl git jq util-linux procps mariadb-client postgresql-client && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy project
COPY ./app/ .
COPY docker/entrypoint.app.sh /entrypoint.app.sh
COPY docker/entrypoint.worker-jobs.sh /entrypoint.worker-jobs.sh
COPY docker/healthcheck-worker-jobs.sh /healthcheck.worker-jobs.sh
COPY docker/entrypoint.worker-monitor.sh /entrypoint.worker-monitor.sh

# Install dependencies at build time (fast import verify + startup on small VPS)
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV HOME=/app
RUN uv sync --frozen

# Set permissions
RUN chown -R appuser:appgroup /app /tmp/uv-cache
RUN chmod 0755 /entrypoint.app.sh /entrypoint.worker-jobs.sh /entrypoint.worker-monitor.sh /healthcheck.worker-jobs.sh

ENV UV_CACHE_DIR=/tmp/uv

# Switch to non-root user
USER appuser

EXPOSE 8000
