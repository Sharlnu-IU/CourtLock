FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=0 UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.13.9
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
ENV PATH="/app/.venv/bin:$PATH"

FROM base AS runtime
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv sync --frozen --no-dev
EXPOSE 8000
CMD ["uvicorn", "--factory", "courtlock.main:create_app", "--host", "0.0.0.0", "--port", "8000"]

# Used by `docker compose --profile test run --rm test`: same image plus dev tools and tests.
FROM base AS dev
RUN uv sync --frozen --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY tests ./tests
COPY scripts ./scripts
RUN uv sync --frozen
CMD ["pytest"]
