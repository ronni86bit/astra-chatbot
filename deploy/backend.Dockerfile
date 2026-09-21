# Backend image: FastAPI service wrapping the deterministic catalogue engine.
#
# The API key is NEVER baked into the image - it is injected at runtime
# (docker compose reads .env; see compose `env_file`). The workbook is the
# read-only data source and is baked in for a self-contained image.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer cache).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .[llm,api]

# The audited workbook is the catalogue source of truth (read-only).
COPY SystemVue_RF_Unique_Question_Assistant_V63.xlsx ./

EXPOSE 8000

# create_app_factory builds the real service (workbook + LLM client) once.
CMD ["uvicorn", "rf_catalogue.api:create_app_factory", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
