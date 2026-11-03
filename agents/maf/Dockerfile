FROM ghcr.io/astral-sh/uv:0.11.22 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY shared /app/shared
COPY agents/maf /app/agents/maf
RUN uv sync --project /app/agents/maf --locked --no-dev --no-editable \
    && useradd --uid 10001 --create-home agent
USER agent
ENV PATH="/app/agents/maf/.venv/bin:$PATH" APP_ENV=azure PORT=3978
EXPOSE 3978
CMD ["teams-maf"]
