# Brick Borrow watcher — single-stage image.
#
# Deliberately NOT a multi-stage build: the only dependency is httpx (pure
# Python + a couple of small wheels), so there is no compile step to separate
# out and a builder stage would add complexity for ~0 MB saved. Blue Plaque
# Hunter's multi-stage build earns its keep because it compiles a frontend;
# this doesn't.
#
# Notably absent: any browser. See docs/RESEARCH.md — the store's catalog API is
# readable anonymously, so unlike the tennis bot this image needs no Playwright,
# no Chromium, and no credentials.

FROM python:3.12-slim

# PYTHONUNBUFFERED so logs reach `docker logs` immediately rather than sitting
# in a block buffer, which makes a 30-minute-cadence service look dead.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, as their own layer: source changes then don't invalidate
# the pip install. This is Docker layer caching, and it's why COPY . . comes last.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Run as a non-root user. If the process is ever compromised it lands as `bot`,
# not root, and it cannot write anywhere except the data volume.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown -R bot:bot /data /app
USER bot

ENV PYTHONPATH=/app/src \
    LEGOBOT_DB_PATH=/data/legobot.sqlite3

VOLUME ["/data"]

# No EXPOSE and no ports: Telegram long polling is an OUTBOUND connection, so
# nothing needs to reach this container from outside.

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import sys,os; sys.exit(0 if os.path.exists('/data') else 1)"

CMD ["python", "-m", "legobot"]
