FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project structure
COPY . .

# Add /app to path so it can find orchestrator.app
ENV PYTHONPATH=/app

EXPOSE 9000

# Run from /app so orchestrator/app.py is found
CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "9000"]