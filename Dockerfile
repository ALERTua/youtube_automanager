FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS production

LABEL maintainer="ALERT <alexey.rubasheff@gmail.com>"

ENV PORT=8080
ENV VERBOSE=0
ENV LOGFILES=False
ENV TELEGRAM_CHAT_ID=""
ENV TELEGRAM_BOT_TOKEN=""
ENV TELEGRAM_ANNOUNCE="False"
ENV DISCORD_WEBHOOK_URL=""
ENV SLACK_WEBHOOK_URL=""
ENV SLACK_CHANNEL=""
ENV SLACK_USER_MENTIONS=""
ENV TEAMS_WEBHOOK_URL=""
ENV TEAMS_USER_MENTIONS=""


EXPOSE $PORT
VOLUME ["/app/config"]

ENV \
    # uv
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_NO_PROGRESS=true \
    UV_CACHE_DIR=.uv_cache \
    # Python
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    # pip
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    # app
    APP_DIR=/app \
    SOURCE_DIR_NAME=youtube_automanager


WORKDIR $APP_DIR

RUN --mount=type=cache,target=$UV_CACHE_DIR \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --no-install-project --no-dev

COPY $SOURCE_DIR_NAME $SOURCE_DIR_NAME

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
        CMD curl localhost:${PORT}/health || exit 1

ENTRYPOINT []

CMD uv run python -m youtube_automanager.runners.automanage
