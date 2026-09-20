FROM python:3.11-slim

WORKDIR /app

RUN pip install poetry

ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

COPY pyproject.toml poetry.lock ./
# Server dependencies only (the "main" group). The pipeline and dashboard need
# pandas, numpy, plotly and streamlit, which the server never imports -- installing
# them made cold builds slow enough to exhaust a small free-tier VM.
RUN poetry install --no-root --only main

# Only what the server runs. server_db.py finds its schema at ../../sql relative to
# itself, hence sql/ sitting next to src/.
COPY src/server_scripts/ ./src/server_scripts/
COPY sql/server_tables.sql ./sql/server_tables.sql

CMD ["python", "src/server_scripts/main.py"]
