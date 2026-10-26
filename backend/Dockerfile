# Gateway image. Build from this folder:  docker build -t gateway:dev .
# Two stages: the first installs dependencies and the package; the second copies only the result,
# so no build tools, no uv and no source tree end up in the image that runs.

FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first: this layer is rebuilt only when pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then our code, installed as a normal package (not editable) so the runtime needs no source folder.
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim-bookworm
# A fixed non-root user. /data is the only place the gateway writes (SQLite files, incident log).
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin gateway \
 && mkdir /data && chown gateway:gateway /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_DB_URL=sqlite:////data/gateway.db \
    GATEWAY_JOBS_DB_URL=sqlite:////data/gateway_jobs.db \
    GATEWAY_INCIDENT_LOG=/data/incidents.jsonl
WORKDIR /data
USER 10001:10001
EXPOSE 8000
VOLUME /data
# No HEALTHCHECK on purpose: Kubernetes probes call /healthz and /readyz.
# Secrets (GATEWAY_SECRET_KEY, ...) come from the environment at run time, never from the image.
CMD ["gateway"]
