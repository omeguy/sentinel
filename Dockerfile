FROM python:3.10-slim

WORKDIR /app

# Install system dependencies if needed (ca-certificates for API calls)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project (This includes the 'ui' folder and 'orchestrator' code)
COPY . .

# Ensure the app can find the local modules like db, schema, etc.
ENV PYTHONPATH=/app/orchestrator:/app

EXPOSE 9000

# Start the unified engine
CMD ["uvicorn", "orchestrator.app:app", "--host", "0.0.0.0", "--port", "9000"]