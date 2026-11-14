# CI tests this version: keep the `image: true` leg in `.github/workflows/test.yml` in step
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY . .
# Non-editable: the image runs the installed package, as a wheel would.
RUN uv sync --frozen --no-dev --no-editable

# CI tests this version: keep the `image: true` leg in `.github/workflows/test.yml` in step
FROM python:3.14-slim

WORKDIR /app

RUN useradd --no-create-home --uid 1000 monitor

COPY --from=builder --chown=1000:1000 /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    MONITOR_CONFIG=/app/config.yaml

USER 1000

EXPOSE 5002

CMD ["sh", "-c", "monitor migrate && exec monitor serve"]
