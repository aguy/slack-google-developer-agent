FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

RUN useradd -m appuser \
    && chown -R appuser:appuser /app
USER appuser

# Use 1 worker with threads — MCP needs a single shared event loop per process
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "120", "main:app"]