FROM python:3.12-slim

# The worker keeps London time (TZ in docker-compose.yml), as the pipeline
# always has; the slim image has no time zone data of its own.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install poetry

ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

COPY pyproject.toml poetry.lock ./
# The server's dependencies ("main") and the daily run's ("worker"). The dashboard
# needs pandas, numpy, plotly and streamlit, which neither imports -- installing
# them made cold builds slow enough to exhaust a small free-tier VM.
RUN poetry install --no-root --only main,worker

# Only what the server and the worker run -- not the dashboard, which is the
# PC's. server_db.py finds its schema at ../../sql relative to itself, hence
# sql/ sitting next to src/.
COPY src/server/ ./src/server/
COPY src/pipeline/ ./src/pipeline/
COPY sql/postgres/schema.sql ./sql/postgres/schema.sql

CMD ["python", "src/server/main.py"]
