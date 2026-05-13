FROM python:3.10-slim

WORKDIR /app

RUN pip install poetry

ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root

COPY src/ ./src/
COPY sql/ ./sql/

CMD ["python", "src/server_scripts/main.py"]
