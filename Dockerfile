FROM python:3.12-slim

WORKDIR /app

# Install platform dependencies (t3nets-sdk pulled from PyPI)
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    "t3nets-sdk>=0.1,<0.2" \
    pyyaml boto3 "starlette>=0.41" "uvicorn[standard]>=0.32"

# Copy application code
COPY agent/ agent/
COPY adapters/ adapters/
COPY version.txt .

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run the AWS server
CMD ["python", "-m", "adapters.aws.server"]
