FROM python:3.13-slim

WORKDIR /app

# Install the package (deps first for layer caching).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# SQLite ledger lives here; mount it as a volume to persist across restarts.
RUN mkdir -p /app/data
ENV POLYBOT_DB_PATH=/app/data/polybot.sqlite3 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["polybot"]
CMD ["run", "--strategy", "micro", "--interval", "900"]
