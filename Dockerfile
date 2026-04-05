FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir . 2>/dev/null || \
    pip install --no-cache-dir \
      "fastapi>=0.115.0" \
      "uvicorn[standard]>=0.30.0" \
      "litellm>=1.40.0" \
      "httpx>=0.27.0" \
      "aiosqlite>=0.20.0" \
      "pyyaml>=6.0" \
      "markdown-it-py>=3.0.0" \
      "apscheduler>=3.10.0" \
      "pydantic>=2.0.0"

# Copy source
COPY . .

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
