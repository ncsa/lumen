# CI tests this version: keep the `image: true` leg in `.github/workflows/test.yml` in step
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# CI tests this version: keep the `image: true` leg in `.github/workflows/test.yml` in step
FROM python:3.14-slim

WORKDIR /app

RUN useradd --no-create-home --uid 1000 lumen

COPY --from=builder --chown=1000:1000 /app /app

ARG APP_VERSION=develop
ARG GIT_COMMIT=N/A
ENV APP_VERSION=${APP_VERSION} \
    GIT_COMMIT=${GIT_COMMIT} \
    PATH="/app/.venv/bin:$PATH"

USER 1000

EXPOSE 5001

ENTRYPOINT ["sh", "entrypoint.sh"]
