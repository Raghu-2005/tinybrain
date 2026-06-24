# Agent is a simple Python worker — no web server needed, just python agent.py
FROM python:3.12-slim

WORKDIR /app

# duckdb pip wheel needs gcc to compile its C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
 && rm -rf /var/lib/apt/lists/*

# Layer-cache pip install separately from source copy
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data is where the DuckDB file lives — mounted as a volume per-agent
# so data persists across container restarts (useful in real deployments)
RUN mkdir -p /data

# The agent is a plain script — no web server, no open ports.
# All traffic is OUTBOUND to the control plane. No EXPOSE needed.
CMD ["python", "agent.py"]
