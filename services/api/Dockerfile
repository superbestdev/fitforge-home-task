# Python 3.12 rather than the host's 3.14: the OCR/PDF stack has the most
# reliable wheel coverage here, and pinning the container decouples us from
# whatever the developer happens to have installed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# tesseract + ghostscript + unpaper are what ocrmypdf shells out to for the
# scanned-manual path. qpdf handles the PDF rewriting.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        ghostscript \
        qpdf \
        unpaper \
        pngquant \
        libjpeg62-turbo \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY db /app/db
COPY services /app/services
COPY seed /app/seed
COPY evals /app/evals
COPY tests /app/tests

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "services.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
