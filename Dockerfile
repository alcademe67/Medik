FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Unbuffered logs so `docker logs` shows output immediately.
ENV PYTHONUNBUFFERED=1

# Default: run the trading engine. docker-compose overrides for the dashboard.
CMD ["python", "-m", "bot.live_engine"]
