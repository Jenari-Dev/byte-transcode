FROM python:3.11-slim

LABEL maintainer="TheOldSchoolModder"
LABEL description="Byte Transcode Server - Media transcoding queue manager"

# Install ffprobe for media scanning
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

# Create app directories
RUN mkdir -p /app /config /config/logs /app/static

WORKDIR /app

# Install Python dependencies
RUN pip install --no-cache-dir flask requests

# Copy server files
COPY byte_server.py /app/byte_server.py
COPY static/ /app/static/

# Environment
ENV BYTE_DB_PATH=/config/byte_transcode.db
ENV BYTE_LOG_DIR=/config/logs
ENV BYTE_STATIC_DIR=/app/static

EXPOSE 5800

VOLUME ["/config", "/media"]

ENTRYPOINT ["python3", "/app/byte_server.py"]
CMD ["--port", "5800"]
