# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.15
# empty: the newest uv; a version such as 0.13.0: that uv
ARG UV_VERSION=
ARG UV_IMAGE=ghcr.io/astral-sh/uv:${UV_VERSION:+${UV_VERSION}-}python${PYTHON_VERSION}-trixie-slim
ARG APP_DIR=/app
ARG UV_CACHE_DIR=/uv-cache
ARG PUID=1000
ARG PGID=1000


# -----------------------------------------------------------------
FROM ${UV_IMAGE} AS deps

ARG APP_DIR
ARG UV_CACHE_DIR

ENV \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_INSTALLER_METADATA=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_NO_PROGRESS=true \
    UV_NO_DEV=true \
    UV_CACHE_DIR=$UV_CACHE_DIR \
    UV_PROJECT_ENVIRONMENT=$APP_DIR/.venv

WORKDIR $APP_DIR

# Only the dependencies: the image copies this venv as its biggest layer, which a change of the code must not touch.
# A new run of this step, for example after a new uv image, must give the same bytes, so the image keeps the pushed layer.
# For that, uv writes no installer metadata, and one compileall process writes the bytecode with source hashes, not times.
RUN --mount=type=cache,target=$UV_CACHE_DIR \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --no-install-project \
    && $APP_DIR/.venv/bin/python -W ignore::SyntaxWarning -m compileall -q -f --invalidation-mode unchecked-hash $APP_DIR/.venv/lib


# -----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS runtime

ARG APP_DIR
ARG PUID
ARG PGID

LABEL maintainer="ALERT <alexey.rubasheff@gmail.com>"
LABEL org.opencontainers.image.description="Adds YouTube videos to your playlists by rules, with a web UI"

# HOME is the data folder of the service: the database and the yt-dlp releases of the updater live there.
# The pages store their values in the data folder too, because the service user cannot write to the code folder.
# The updater runs uv rarely, so uv keeps no cache.
ENV \
    PATH=$APP_DIR/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    HOME=/data \
    NICEGUI_STORAGE_PATH=/data/.nicegui \
    PORT=8080 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_PROGRESS=1

RUN groupadd --gid ${PGID} appuser \
    && useradd --uid ${PUID} --gid ${PGID} --no-log-init --no-create-home appuser \
    && mkdir -p /data \
    && chown ${PUID}:${PGID} /data

# The yt-dlp updater installs new releases with uv while the service runs.
COPY --from=deps /usr/local/bin/uv /usr/local/bin/uv

WORKDIR $APP_DIR

# --link keeps the layer of the packages when the python image changes.
# pyvenv.cfg names the uv version, so it goes with the rest of the venv into a small separate layer.
COPY --link --from=deps $APP_DIR/.venv/lib $APP_DIR/.venv/lib
COPY --link --from=deps --exclude=lib $APP_DIR/.venv $APP_DIR/.venv

# The code goes into its own small layer, so a change of the code does not push the dependencies again.
COPY youtube_automanager youtube_automanager
RUN python -m compileall -q --invalidation-mode unchecked-hash youtube_automanager

EXPOSE 8080

VOLUME /data

USER ${PUID}:${PGID}

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "youtube_automanager.healthcheck"]

CMD ["python", "-m", "youtube_automanager", "serve"]
