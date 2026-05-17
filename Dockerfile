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

EXPOSE 5001

ENTRYPOINT ["sh", "entrypoint.sh"]
