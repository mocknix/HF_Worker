FROM python:3.12-slim

# ffmpeg for yt-dlp robustness; nodejs as a JS runtime so yt-dlp can run
# YouTube's player JS challenges (otherwise extraction breaks on modern videos).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && node --version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "worker.py"]
