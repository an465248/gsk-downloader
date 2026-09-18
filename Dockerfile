FROM python:3.12-slim

# ffmpeg = server-merge fallback ke liye (default download user device pe hota hai)
# nodejs = yt-dlp JS-challenge (n-param) solve karega -> UNS throttled URLs ki jagah
# FULL-SPEED direct links. Iske bina YouTube har connection ko KB/s me ghont deta hai.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg nodejs \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
