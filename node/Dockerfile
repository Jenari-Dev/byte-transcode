FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

LABEL maintainer="TheOldSchoolModder"
LABEL description="Byte Transcode Node - GPU-accelerated media transcoding worker"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    wget \
    curl \
    mkvtoolnix \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Jellyfin FFmpeg (has NVENC support out of the box)
RUN wget -q https://repo.jellyfin.org/releases/ffmpeg/7.1.2-1/jellyfin-ffmpeg7_7.1.2-1-noble_amd64.deb \
    && dpkg -i jellyfin-ffmpeg7_7.1.2-1-noble_amd64.deb || apt-get install -f -y \
    && rm jellyfin-ffmpeg7_7.1.2-1-noble_amd64.deb \
    && ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe

# Install dovi_tool
RUN ARCH=$(dpkg --print-architecture) && \
    wget -q "https://github.com/quietvoid/dovi_tool/releases/download/2.2.1/dovi_tool-2.2.1-x86_64-unknown-linux-musl.tar.gz" -O /tmp/dovi_tool.tar.gz && \
    tar xzf /tmp/dovi_tool.tar.gz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/dovi_tool && \
    rm /tmp/dovi_tool.tar.gz

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages requests

# Create directories
RUN mkdir -p /app /temp/byte_work

WORKDIR /app

# Copy node files
COPY byte_node.py /app/byte_node.py

# Verify tools
RUN ffmpeg -version | head -1 && \
    dovi_tool --version && \
    mkvmerge --version | head -1 && \
    echo "All tools verified"

VOLUME ["/temp", "/media"]

ENTRYPOINT ["python3", "/app/byte_node.py"]
CMD ["--server", "http://byte-server:5800", "--name", "ByteNode-1", "--gpu", "RTX 5080"]
