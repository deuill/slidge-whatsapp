ARG PYTHONVERSION=3.13
ARG DEBIANVERSION=trixie

# *******
# Builder
# *******
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

ENV GOBIN="/usr/local/bin"
COPY --from=codeberg.org/slidge/nocache-apt /apt-install-nocache /usr/local/bin/apt-install-nocache
# git for setuptools-scm, plus basic build tools for potentially missing wheels
RUN apt-install-nocache \
    git \
    make \
    build-essential \
    ca-certificates \
    cargo \
    curl \
    git \
    gcc \
    golang \
    g++ \
    libffi-dev \
    libssl-dev \
    pkg-config \
    python3-dev \
    libmupdf-dev \
    libgumbo-dev \
    libfreetype6-dev \
    libharfbuzz-dev \
    libjbig2dec0-dev \
    libjpeg62-turbo-dev \
    libmujs-dev \
    libopenjp2-7-dev \
    rustc

WORKDIR /build
ENV UV_PROJECT_ENVIRONMENT=/venv
RUN uv venv $UV_PROJECT_ENVIRONMENT

# install dependencies in /venv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --no-install-project

COPY build.py ./
COPY --exclude=*/generated --exclude=*/.gopy.sum slidge_whatsapp slidge_whatsapp
# .git/ needs to be mounted for setuptools-scm to set the version
ARG SLIDGE_BUMP_DEPS=""
ARG SLIDGE_PRERELEASE=""
RUN --mount=source=.git,target=/build/.git,type=bind \
    set -x && \
    uv sync --no-dev ${SLIDGE_BUMP_DEPS:+--upgrade} && \
    if [ -n "$SLIDGE_PRERELEASE" ]; then \
        uv sync --upgrade-package slidge --no-dev --prerelease=allow; \
    fi

# ************
# CI container
# ************
FROM builder AS ci

# We won't use this venv in CI, but this populates the uv cache in the container,
# minimizing (or even nulling) downloads.
RUN --mount=source=.git,target=/build/.git,type=bind \
    uv sync --all-groups --no-install-project
ENV UV_PROJECT_ENVIRONMENT="/woodpecker/src/codeberg.org/slidge/slidge-whatsapp/.venv"
ENV UV_LINK_MODE=copy
ENV PATH="$UV_PROJECT_ENVIRONMENT/bin:$PATH"

# *******************************
# Base container for prod
# *******************************
FROM docker.io/python:$PYTHONVERSION-slim-$DEBIANVERSION AS common

ENV PYTHONUNBUFFERED=1
ENV PATH="/venv/bin:$PATH"
STOPSIGNAL SIGINT
WORKDIR /var/lib/slidge

COPY --from=codeberg.org/slidge/nocache-apt /apt-install-nocache /usr/local/bin/apt-install-nocache
# libmagic1: to guess mime type from files
# media-types: to determine file name suffix based on file type
# libmupdf25.1: support for PDF file previews
# media-types: to determine file name suffix based on file type
RUN apt-install-nocache libmagic1 media-types shared-mime-info libmupdf25.1 ffmpeg

COPY --from=builder /venv /venv

# *************
# Dev container
# *************
FROM ci AS dev

# copy "localhost" certs from the prosody slidge dev container, so
COPY --from=codeberg.org/slidge/prosody-slidge-dev:latest \
  /etc/prosody/certs/localhost.crt \
  /usr/local/share/ca-certificates/
RUN update-ca-certificates

ENV PATH="/venv/bin:$PATH"
ENV GOBIN="/usr/local/bin"

RUN ln -s /venv/lib/python3.* /venv/lib/python
RUN apt-install-nocache python3-watchdog libmagic1 media-types shared-mime-info
RUN go install -v github.com/go-python/gopy@master
RUN go install golang.org/x/tools/cmd/goimports@latest

COPY watcher.py /build/

ENTRYPOINT ["/usr/bin/python3", "/build/watcher.py", \
            "/venv/lib/python/site-packages/slidge:/build/slidge_whatsapp", \
            "--dev-mode", "--debug", \
            "--legacy-module=slidge_whatsapp", "--jid=slidge.localhost", "--secret=secret"]

# **************
# Prod container
# **************
FROM common AS slidge-whatsapp

RUN ln -s /venv/lib/python3.* /venv/lib/python

RUN addgroup --system --gid 10000 slidge
RUN adduser --system --uid 10000 --ingroup slidge --home /var/lib/slidge slidge
USER slidge

COPY --from=builder /build/slidge_whatsapp /venv/lib/python/site-packages/slidge_whatsapp

ENTRYPOINT ["slidge-whatsapp"]
