# Single image for both the web (ingest API + status page + admin portal) and the
# worker (LibreOffice conversion pipeline). The entrypoint branches on APP_ROLE.
#
#   docker build -t crewmeal .
#   docker run -e APP_ROLE=web    -p 8000:8000 crewmeal   # FastAPI
#   docker run -e APP_ROLE=worker                 crewmeal   # queue worker
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SOFFICE_PATH=/usr/bin/soffice \
    APP_ROLE=web \
    PORT=8000

# System dependencies:
#  * libreoffice-impress: headless PPTX -> PDF conversion (worker pipeline).
#  * fonts-noto-cjk: render Korean slide text correctly during PDF rendering.
#  * fonts-dejavu-core: baseline Latin fallback fonts.
#  * curl: container health checks.
# PyMuPDF and psycopg[binary] ship self-contained wheels, so no extra libs needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        fonts-noto-cjk \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

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
