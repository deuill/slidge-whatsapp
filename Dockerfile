ARG PYTHONVER=3.11
## Base build stage for Slidge, prepares and installs common dependencies.
FROM docker.io/library/python:$PYTHONVER-bookworm AS builder
ARG PYTHONVER
ENV PATH="/venv/bin:/root/.local/bin:$PATH"
ENV UV_PROJECT_ENVIRONMENT=/venv/
ENV GOBIN="/usr/local/bin"

# rust/cargo is for building "cryptography" since they don't provide wheels for arm32
RUN echo "deb http://deb.debian.org/debian bookworm-backports main" >> /etc/apt/sources.list \
    && apt-get update -y && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cargo \
    curl \
    git \
    gcc \
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
    rustc \
    && apt-get install -y golang -t bookworm-backports

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /build

RUN go install -v github.com/go-python/gopy@master
RUN go install golang.org/x/tools/cmd/goimports@latest

COPY uv.lock pyproject.toml /build/

RUN uv venv /venv --relocatable
RUN uv sync --frozen --no-install-project --no-dev
RUN ln -s /venv/lib/python$PYTHONVER /venv/lib/python

COPY ./slidge_whatsapp/*.go ./slidge_whatsapp/go.* /build/
COPY ./slidge_whatsapp/media /build/media

ENV CGO_LDFLAGS="-lgumbo -lfreetype -ljbig2dec -lharfbuzz -ljpeg -lmujs -lopenjp2"
# for this base container we don't install all development dependencies,
# but we need pybindgen for gopy anyway
# We could
RUN uv pip install pybindgen
RUN gopy build -output=generated -no-make=true -build-tags="mupdf extlib static" /build/

# CI environment for slidge
FROM builder AS woodpecker-slidge-whatsapp
# In CI we sometimes move /venv to .venv, then update it for the whole workflow.
ENV PATH=".venv/bin:$PATH"
RUN uv sync --frozen --no-install-project --all-extras

FROM codeberg.org/slidge/slidge-base AS slidge-whatsapp

USER root
RUN apt update -y && apt install -y ffmpeg libgumbo1 libfreetype6 libharfbuzz0b libjbig2dec0 libjpeg62-turbo libmujs2 libopenjp2-7

COPY --from=builder /venv /venv
COPY ./slidge_whatsapp/*.py /venv/lib/python/site-packages/legacy_module/
COPY --from=builder /build/generated /venv/lib/python/site-packages/legacy_module/generated

USER slidge

FROM builder AS slidge-whatsapp-dev

COPY --from=codeberg.org/slidge/prosody-slidge-dev:latest /etc/prosody/certs/localhost.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates

RUN apt update -y && apt install -y ffmpeg
RUN uv pip install watchdog[watchmedo]
ENV SLIDGE_LEGACY_MODULE=slidge_whatsapp

COPY ./watcher.py /
USER root

ENTRYPOINT ["python", "/watcher.py", "/venv/lib/python/site-packages/slidge:/venv/lib/python/site-packages/slidge_whatsapp", "--dev-mode", "--log-format", "%(levelname)s:%(threadName)s:%(name)s:%(message)s"]

# wheel builder
# docker buildx build . --target wheel \
# --platform linux/arm64,linux/amd64 \
# -o ./dist/
FROM builder AS builder-wheel

COPY LICENSE README.md /build/
COPY slidge_whatsapp/go.* /build/slidge_whatsapp/
COPY slidge_whatsapp/*.py /build/slidge_whatsapp/
COPY slidge_whatsapp/*.go /build/slidge_whatsapp/
COPY slidge_whatsapp/media/*.go /build/slidge_whatsapp/media/
COPY build.py /build/

RUN uv build
RUN ls -l ./dist
RUN python --version

FROM scratch AS wheel
COPY --from=builder-wheel ./build/dist/* /
