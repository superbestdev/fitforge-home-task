FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0 pydantic==2.10.4

COPY services/mockpsp /app/services/mockpsp
ENV PYTHONPATH=/app

EXPOSE 8090

CMD ["uvicorn", "services.mockpsp.main:app", "--host", "0.0.0.0", "--port", "8090"]
