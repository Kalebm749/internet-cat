# One slim image for the engine, web and devdash services. Pure-Python deps only.
#   docker build --target runtime -t internet-cat:local .    (compose does this for you)
#   docker build --target test .                             (runs the test suite)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CAT_CONFIG_DIR=/app/config

# tzdata so TZ= works (the website shows real times such as "brain last seen")
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. UID/GID should match the host user that owns ./data (default 1000).
ARG CAT_UID=1000
ARG CAT_GID=1000
RUN groupadd --gid "$CAT_GID" cat \
 && useradd --uid "$CAT_UID" --gid "$CAT_GID" --create-home --shell /usr/sbin/nologin cat

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY cat ./cat
COPY scripts ./scripts
COPY config ./config
COPY content ./content
RUN mkdir -p /app/data && chown -R cat:cat /app/data


# ---- test stage: pytest + the tests (not part of the runtime image) ----
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests ./tests
USER cat
CMD ["pytest", "-q"]


# ---- runtime stage (the default target) ----
FROM base AS runtime
USER cat
# Each compose service overrides the command (engine: python -m cat.nit; web and devdash: uvicorn ...).
CMD ["python", "-m", "cat.nit"]
