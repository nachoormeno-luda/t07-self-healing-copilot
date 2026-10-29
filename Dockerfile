# Cloud Run image for the A2A server (a2a_server.py).
# Google buildpacks no longer ship Python 3.12 (only 3.13+), while this repo pins 3.12 for
# cloudpickle parity with Vertex AI Agent Runtime — so we build from the official 3.12 image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# .gcloudignore already strips .venv/.git/.env/tests state from the upload.
COPY . .

EXPOSE 8080
CMD ["python", "a2a_server.py"]
