FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

RUN apt-get update -qy
RUN apt-get install -qyy -o APT::Install-Recommends=false -o APT::Install-Suggests=false ca-certificates \
    git wget

WORKDIR /app

# Copy only dependency files first to leverage Docker layer caching and ensure
# dependencies (including google-api-python-client) are installed in the image
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy the full application source
COPY . /app

FROM python:3.12-slim-bookworm

COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src:${PYTHONPATH:-}"

WORKDIR /app

CMD alembic upgrade head && python app/main.py
