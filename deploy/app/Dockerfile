# The parley clock image: Python 3.12 + uv-managed deps + supercronic to fire the weekly
# session (and a daily heartbeat watchdog). No broker logic here — it connects to the
# `gateway` service over the compose network and reads prices/news; fundamentals come
# from EDGAR (free). State lives on mounted volumes, not in the image.
FROM python:3.12-slim-bookworm

# uv from its official image — reproducible, no pip bootstrap. Bump the tag deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# supercronic: a single-binary cron that runs as non-root and logs to stdout (unlike
# system cron) — the right fit for a container scheduler. tzdata so TZ= works.
ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_ARCH=linux-amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-${SUPERCRONIC_ARCH}" \
        -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first so the layer caches until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Application code + the committed ticker->CIK reference map EDGAR needs.
COPY src/ ./src/
COPY data/reference/ticker_cik_historical.json ./data/reference/ticker_cik_historical.json
COPY deploy/app/crontab ./deploy/app/crontab
COPY deploy/app/entrypoint.sh /usr/local/bin/app-entrypoint.sh
RUN chmod +x /usr/local/bin/app-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/app-entrypoint.sh"]
