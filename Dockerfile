FROM python:3.12-slim

WORKDIR /app

# System deps (minimal — wir lesen nur Files + sprechen HTTP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY ki_os_mcp/ ./ki_os_mcp/

ENV PYTHONUNBUFFERED=1 \
    VAULT_PATH=/vault \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=3002

EXPOSE 3002

CMD ["python", "-m", "ki_os_mcp.server"]
