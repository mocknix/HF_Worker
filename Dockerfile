FROM python:3.12-slim

# ffmpeg is optional but yt-dlp recommends it; tiny size hit, big robustness win
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "worker.py"]
