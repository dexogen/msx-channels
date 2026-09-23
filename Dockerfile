FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 msx && useradd --uid 10001 --gid 10001 --no-create-home msx \
    && mkdir -p /app /media /data/cache /run/msx \
    && chown -R msx:msx /data /run/msx
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY msx_channels ./msx_channels
LABEL org.opencontainers.image.title="MSX Channels" \
      org.opencontainers.image.description="Loop video channels from a mounted directory for Media Station X" \
      org.opencontainers.image.source="https://github.com/dexogen/msx-channels"
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
CMD ["python", "-m", "msx_channels.app"]
