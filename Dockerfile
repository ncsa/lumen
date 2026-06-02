FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ARG APP_VERSION=develop
ARG GIT_COMMIT=N/A
ENV APP_VERSION=${APP_VERSION}
ENV GIT_COMMIT=${GIT_COMMIT}
ENV UV_NO_CACHE=1

RUN useradd --no-create-home --uid 1000 lumen && chown -R lumen /app
USER 1000

EXPOSE 5001

ENTRYPOINT ["sh", "entrypoint.sh"]
