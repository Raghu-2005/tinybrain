# ── Stage: runtime ────────────────────────────────────────────────────────────
# Use slim variant to keep image small. python:3.12-slim is ~50MB vs ~350MB full.
FROM python:3.12-slim

# Set working directory inside the container
WORKDIR /app

# Install system dependencies needed by asyncpg (C extension build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements first — Docker layer caching means pip install only
# reruns when requirements.txt changes, not on every code change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Expose the port Uvicorn will listen on
EXPOSE 8000

# Run Uvicorn directly — no shell wrapper needed for a single-process service.
# --host 0.0.0.0 binds to all interfaces (required inside Docker).
# --workers 1 keeps things simple; production would use multiple workers
# or run behind a process manager like gunicorn.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
