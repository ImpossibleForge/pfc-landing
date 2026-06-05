FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y wget unzip && rm -rf /var/lib/apt/lists/*

# pfc_jsonl binary
RUN wget -q \
    https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-linux-x64 \
    -O /app/pfc_jsonl \
  && chmod +x /app/pfc_jsonl

# DuckDB CLI
RUN wget -q \
    https://github.com/duckdb/duckdb/releases/download/v1.2.2/duckdb_cli-linux-amd64.zip \
    -O /tmp/duckdb.zip \
  && unzip -q /tmp/duckdb.zip -d /usr/local/bin \
  && chmod +x /usr/local/bin/duckdb \
  && rm /tmp/duckdb.zip

# Pre-install pfc community extension (cached in image, no runtime download needed)
RUN duckdb -c "INSTALL pfc FROM community;" || true

RUN mkdir -p /tmp/pfc_sessions

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV PORT=8080
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "180", "--workers", "2", "server:app"]
