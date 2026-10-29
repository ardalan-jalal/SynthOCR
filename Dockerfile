# syntax=docker/dockerfile:1
# Refresh both tags and digests deliberately; runtime dependencies use uv.lock.
FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS base

# Pillow's Linux wheel loads FriBiDi at runtime for RAQM. Keep its Debian license
# and use Debian's signed package metadata and package checksums.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libfribidi0 \
    && rm -rf /var/lib/apt/lists/*

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE-MIT LICENSE-APACHE NOTICE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra web --no-install-project --python /usr/local/bin/python
COPY synthocr ./synthocr
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra web --no-editable --python /usr/local/bin/python

FROM base AS runtime
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY deploy/container.toml ./config.toml
COPY fonts/open/notosans ./fonts/open/notosans
COPY fonts/open/notonaskharabic ./fonts/open/notonaskharabic
RUN groupadd --gid 10001 synthocr \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin synthocr \
    && mkdir -p /data/datasets /data/processed /corpora /extra-fonts \
    && chown -R 10001:10001 /data \
    && python -c "from PIL import features; assert features.check_feature('raqm'), 'RAQM is required'" \
    && synthocr fonts fetch --family notosans --family notonaskharabic \
    && synthocr fonts check --profile sorani --require-any \
    && synthocr fonts check --profile kurmanji --require-any
USER 10001:10001
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).close()"]
# Use host networking: the UI keeps its existing loopback-only access boundary.
ENTRYPOINT ["synthocr", "--config", "/app/config.toml"]
CMD ["ui", "--no-browser", "--output-root", "/data/datasets", "--cache-mb", "256"]
