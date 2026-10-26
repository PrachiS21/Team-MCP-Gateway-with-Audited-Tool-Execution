# WooCommerce MCP server image. Build from this folder:  docker build -t woo-mcp:dev .
# Same two-stage pattern as the gateway; this one writes nothing to disk, so it runs with a read-only root filesystem.

FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim-bookworm
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin woo
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
# No HEALTHCHECK on purpose: Kubernetes probes call /healthz and /readyz.
# WOO_BASE_URL, WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET, MCP_SERVER_TOKEN and STORE_TIMEZONE come from the environment.
CMD ["python", "-m", "woo_mcp"]
