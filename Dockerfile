FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV PYTHONPATH=/app/orchestrator

EXPOSE 9000
CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "9000"]