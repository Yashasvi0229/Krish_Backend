# syntax=docker/dockerfile:1.7
# ============================================================================
# GNC Invoice Automation — Backend Dockerfile
# Single-stage image, works for Render's Docker deploys and for local docker-compose.
# ============================================================================

FROM python:3.11-slim

# Prevents Python from writing .pyc files and buffers stdout (better logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps:
#   - libpq5      : needed by asyncpg / psycopg2 at runtime
#   - build-essential + libpq-dev : needed to compile psycopg2 during pip install
#   - tesseract-ocr + poppler-utils : needed later (Step 4) for OCR / PDF → image
# We keep build tools installed because Render's slim images are ephemeral;
# the size cost is small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libpq5 \
        curl \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better Docker layer caching).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the app source.
COPY . .

# Render sets $PORT dynamically; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# On every container start:
#   1. Run pending Alembic migrations. Idempotent — a no-op if the DB is
#      already at head. Runs inside a transaction, so a failed migration
#      leaves the DB untouched and the container exits with a clear error.
#   2. Launch uvicorn.
# `--proxy-headers` respects X-Forwarded-For from Render's load balancer.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
