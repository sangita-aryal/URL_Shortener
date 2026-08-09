FROM python:3.13-slim

# curl is used only by the docker-compose healthcheck probe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies in a separate layer so source-code changes
# don't invalidate the (slower) pip install step.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/     ./app/
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
