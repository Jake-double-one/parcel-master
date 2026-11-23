# syntax=docker/dockerfile:1

FROM node:26-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim AS backend
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/parcelmaster ./parcelmaster
RUN uv sync --frozen --no-dev

FROM python:3.14-slim
# libatomic1: needed by the Node.js that Playwright ships to install the browser.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 parcelmaster \
    && mkdir /data && chown parcelmaster /data
WORKDIR /app
COPY --from=backend /app /app
COPY --from=frontend /build/dist /app/static
ENV PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers \
    PM_DATA_DIR=/data \
    PM_STATIC_DIR=/app/static \
    PYTHONUNBUFFERED=1
# The Amazon module needs a real browser when Amazon answers the sign-in with
# a JavaScript check.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
USER parcelmaster
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/meta', timeout=4)"
# Which addresses may tell uvicorn the client's address and scheme (X-Forwarded-*).
# "*" suits the usual reverse proxy in another container; set your proxy's
# address to stop clients that reach the port directly from making them up.
ENV FORWARDED_ALLOW_IPS="*"
# The commit this image is built from; shown in the log and the settings. Set
# last, so that a new commit does not rebuild the layers above.
ARG PM_BUILD=dev
ENV PM_BUILD=$PM_BUILD
CMD ["uvicorn", "parcelmaster.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
