FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY sql ./sql
COPY tests ./tests

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    WAREHOUSE_PATH=/app/data/warehouse.duckdb

ENTRYPOINT ["python", "-m", "pipeline"]
CMD ["--help"]
