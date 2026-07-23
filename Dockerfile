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

# Optional: stage the Microsoft Information Protection (MIP) File SDK native
# libraries used for decrypting MIP-protected documents. These are
# Microsoft-licensed binaries and are NOT baked in by default. Provide a pinned
# version + SHA-256 at build time to fetch them:
#
#   docker build --build-arg MIP_SDK_VERSION=1.14.107 \
#                --build-arg MIP_SDK_SHA256=<hex-digest-of-nupkg> .
#
# The CLI entrypoint (CREWMEAL_MIP_SDK_CLI) is a deployment-provided thin wrapper
# around the SDK and must be supplied separately; until it is set, MIP decryption
# stays "not configured" and — if an admin enables it — fails loudly rather than
# passing encrypted files through. For local/CI demos, point CREWMEAL_MIP_SDK_CLI
# at the bundled reference tool: "python -m crewmeal.search_enhancement.mip_tool".
ARG MIP_SDK_VERSION=""
ARG MIP_SDK_SHA256=""
ARG MIP_SDK_RUNTIME=linux-x64
ENV CREWMEAL_MIP_SDK_LIB_DIR=/opt/mip/lib \
    CREWMEAL_MIP_RMS_SCOPE=https://aadrm.com/.default
COPY scripts/fetch_mip_sdk.py ./scripts/fetch_mip_sdk.py
RUN if [ -n "$MIP_SDK_VERSION" ]; then \
        python scripts/fetch_mip_sdk.py \
            --version "$MIP_SDK_VERSION" \
            --sha256 "$MIP_SDK_SHA256" \
            --runtime "$MIP_SDK_RUNTIME" \
            --dest "$CREWMEAL_MIP_SDK_LIB_DIR" ; \
    else \
        echo "MIP SDK not fetched (no MIP_SDK_VERSION build-arg); MIP decryption stays disabled until CREWMEAL_MIP_SDK_CLI is configured." ; \
    fi

# Optional: provision the low (no-Vision) analysis tier's OCR. This installs the
# CPU-only OCR engine (rapidocr-onnxruntime + its numpy/opencv/Pillow deps) and
# fetches a Korean recognition model, because the engine's bundled model reads
# Chinese/English only. Kept out of the default image (heavy wheels) and off
# unless requested. When absent, the low tier still extracts text/tables/charts;
# only embedded-image OCR is skipped.
#
#   docker build --build-arg ENABLE_LOW_TIER_OCR=1 \
#                --build-arg OCR_REC_URL=https://huggingface.co/monkt/paddleocr-onnx/resolve/main/languages/korean/rec.onnx \
#                --build-arg OCR_KEYS_URL=https://huggingface.co/monkt/paddleocr-onnx/resolve/main/languages/korean/dict.txt \
#                --build-arg OCR_REC_SHA256=<hex> --build-arg OCR_KEYS_SHA256=<hex> .
ARG ENABLE_LOW_TIER_OCR=""
ARG OCR_REC_URL=""
ARG OCR_KEYS_URL=""
ARG OCR_REC_SHA256=""
ARG OCR_KEYS_SHA256=""
ENV PPTX_OCR_MODEL_DIR=/opt/ocr/korean
COPY scripts/fetch_ocr_model.py ./scripts/fetch_ocr_model.py
RUN if [ -n "$ENABLE_LOW_TIER_OCR" ]; then \
        pip install ".[ocr]" && \
        if [ -n "$OCR_REC_URL" ]; then \
            python scripts/fetch_ocr_model.py \
                --rec-url "$OCR_REC_URL" --rec-sha256 "$OCR_REC_SHA256" \
                --keys-url "$OCR_KEYS_URL" --keys-sha256 "$OCR_KEYS_SHA256" \
                --dest "$PPTX_OCR_MODEL_DIR" ; \
        else \
            echo "Low-tier OCR engine installed, but no Korean model fetched (no OCR_REC_URL); Korean image text will not be read until PPTX_OCR_MODEL_DIR is populated." ; \
        fi ; \
    else \
        echo "Low-tier OCR not provisioned (no ENABLE_LOW_TIER_OCR build-arg); the text_ocr tier runs text-only extraction." ; \
    fi

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
