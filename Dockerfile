FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Polling bot — no ports to expose. BOT_TOKEN (and optional TARGET_CHAT_ID)
# are provided as environment variables by the platform.
CMD ["python", "bot.py"]
