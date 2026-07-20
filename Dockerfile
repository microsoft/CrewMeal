# Single image for both the web (ingest API + status page + admin portal) and the
# worker (LibreOffice + rhwp document pipeline). The entrypoint branches on APP_ROLE.
#
#   docker build -t crewmeal .
#   docker run -e APP_ROLE=web    -p 8000:8000 crewmeal   # FastAPI
#   docker run -e APP_ROLE=worker                 crewmeal   # queue worker
ARG RHWP_COMMIT=8d3bfa4b92174b16bac587fe1409975cf34ba566

FROM rust:1.93.1-bookworm AS rhwp-build

ARG RHWP_COMMIT

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        git \
        libfontconfig1-dev \
        libfreetype6-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src \
    && git -C /src remote add origin https://github.com/edwardkim/rhwp.git \
    && git -C /src fetch --depth=1 origin "${RHWP_COMMIT}" \
    && git -C /src checkout --detach FETCH_HEAD \
    && test "$(git -C /src rev-parse HEAD)" = "${RHWP_COMMIT}"

WORKDIR /src
RUN cargo build --locked --release --features native-skia --bin rhwp

FROM python:3.11-slim-bookworm

ARG RHWP_COMMIT

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SOFFICE_PATH=/usr/bin/soffice \
    RHWP_PATH=/usr/local/bin/rhwp \
    APP_ROLE=web \
    PORT=8000

# System dependencies:
#  * libreoffice-impress: headless PPTX -> PDF conversion (worker pipeline).
#  * fontconfig/freetype: rhwp native-skia font discovery and rendering.
#  * fonts-noto-cjk: render Korean slide text correctly during PDF rendering.
#  * fonts-dejavu-core: baseline Latin fallback fonts.
#  * curl: container health checks.
# PyMuPDF and psycopg[binary] ship self-contained wheels, so no extra libs needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        fonts-noto-cjk \
        fonts-dejavu-core \
        libfontconfig1 \
        libfreetype6 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=rhwp-build /src/target/release/rhwp /usr/local/bin/rhwp

LABEL org.opencontainers.image.source="https://github.com/microsoft/CrewMeal" \
      org.opencontainers.image.rhwp.source="https://github.com/edwardkim/rhwp" \
      org.opencontainers.image.rhwp.revision="${RHWP_COMMIT}" \
      org.opencontainers.image.rhwp.version="0.7.19"

WORKDIR /app

# Install the Python package. Copying the build inputs first lets Docker cache the
# (slow) dependency install layer across source-only changes.
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Role-branching entrypoint. Strip any CR characters so the script runs even if
# it was checked out with Windows (CRLF) line endings.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# Run as a non-root user with a writable home (LibreOffice + local artifact/db paths).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser
ENV HOME=/home/appuser \
    CREWMEAL_SEARCH_DB=/data/search-enhancement.db

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
