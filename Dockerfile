# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12.7-slim-bookworm
ARG NODE_IMAGE=node:22.14.0-bookworm-slim
ARG NGINX_IMAGE=nginx:1.27.4-alpine

FROM ${PYTHON_IMAGE} AS backend-build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=120
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    attempts=0; \
    until python -m pip install --retries 10 --timeout 120 --prefix=/install .; do \
        attempts=$((attempts + 1)); \
        test "$attempts" -lt 5 || exit 1; \
        sleep 5; \
    done

FROM ${PYTHON_IMAGE} AS backend-core
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN python -m venv /opt/venv
COPY --from=backend-build /install /usr/local
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN addgroup --system app && adduser --system --ingroup app --uid 10001 app \
    && chown -R app:app /app
USER app
CMD ["uvicorn", "src.api_view.web_main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

FROM backend-core AS ingestion

FROM backend-core AS export
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium pandoc \
    && rm -rf /var/lib/apt/lists/*
USER app

FROM ${PYTHON_IMAGE} AS sandbox
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN addgroup --system sandbox && adduser --system --ingroup sandbox --uid 65532 sandbox
WORKDIR /workspace
USER sandbox
ENTRYPOINT ["python"]

FROM ${NODE_IMAGE} AS frontend-build
WORKDIR /frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend ./
RUN pnpm build

FROM ${NGINX_IMAGE} AS frontend
COPY ops/nginx/frontend.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /frontend/dist /usr/share/nginx/html

FROM ${NGINX_IMAGE} AS nginx-edge
COPY ops/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ops/nginx/proxy-headers.conf /etc/nginx/proxy-headers.conf
