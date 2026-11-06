# obi serve as a service (ADR-014; runbook: docs/deploy.md).
# One container serves one Git-backed project. Releases and data live on the /srv/obi volume:
# a new image or a deploy never touches the project database or the users.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 obi \
    && mkdir -p /srv/obi && chown obi:obi /srv/obi
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --frozen --no-dev --no-editable && rm -rf /root/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    OBI_HOME=/srv/obi \
    OBI_HOST=0.0.0.0 \
    OBI_PORT=8765 \
    OBI_GIT_BRANCH=main
# Required at run time: OBI_GIT_URL (and OBI_GIT_PATH when the project is not at the repository root).
# Secrets: OBI_SECRET_KEY (session cookies) and OBI_DEPLOY_TOKEN (obi deploy, webhooks); when
# unset, both are generated into /srv/obi once.

USER obi
VOLUME /srv/obi
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/_health', timeout=4)"
CMD ["obi", "serve"]
