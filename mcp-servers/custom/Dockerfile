FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the DuckDB FTS extension into /root/.duckdb/extensions/ so the
# container doesn't need network at startup and doesn't crash-loop when LOAD
# fails.
RUN python3 -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL fts'); c.execute('LOAD fts')"

COPY server.py queries.py sql_guard.py wikipedia.py stubgen.py ./

VOLUME ["/data", "/logs", "/vault"]

EXPOSE 8765

CMD ["python", "server.py"]
