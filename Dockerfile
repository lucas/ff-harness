# Python 3.14 matches the dev venv; uv-managed deps; sync --frozen --no-dev for prod.
FROM python:3.14-slim AS base

# curl for the uv installer, git for the post-hook sandbox commits, ca-certificates
# so httpx can talk to OpenRouter.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv via the official installer (small, fast, no compile).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Copy lockfile + project metadata first so dependency resolution is cached
# independently of source changes.
COPY pyproject.toml uv.lock /app/

# Install production dependencies only. --frozen pins to uv.lock; --no-dev skips
# the pytest/pytest-asyncio group. --no-install-project so the wheel build of
# the local package doesn't fire before harness/ is copied in.
RUN uv sync --frozen --no-dev --no-install-project

# Copy the application and the example env file (operators may want to peek at
# it inside the container).
COPY harness /app/harness
COPY .env.example /app/.env.example

# Install the project itself now that the source is present.
RUN uv sync --frozen --no-dev

# Pre-create the data directories so the bind-mount target exists even when
# the host directory is empty.
RUN mkdir -p /app/data/sessions /app/data/sites

EXPOSE 8000

# Hit "/" (the session-list page); HTTP 200 means the app is up.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/').status == 200 else sys.exit(1)"

CMD ["uv", "run", "uvicorn", "harness.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
