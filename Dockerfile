# Tesseract API and graph builder. No secrets are baked in: keys and passwords
# arrive at runtime from the environment (see .env.example).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    API_HOST=0.0.0.0 \
    TESSERACT_STATE=/var/lib/tesseract

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tesseract/ tesseract/
RUN pip install . \
 && useradd --system --uid 10001 tesseract \
 && mkdir -p /var/lib/tesseract && chown tesseract /var/lib/tesseract
USER tesseract

EXPOSE 8091
CMD ["python", "-m", "tesseract.api"]
