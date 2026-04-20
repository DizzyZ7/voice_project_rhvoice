FROM python:3.11-slim

# Set a working directory
WORKDIR /app

# Install system dependencies needed for RHVoice
RUN apt-get update && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/Components: main/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i 's/main$/main contrib non-free non-free-firmware/' /etc/apt/sources.list; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        alsa-utils ca-certificates espeak-ng rhvoice rhvoice-russian wget \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the service code into the image
COPY . .

# By default, the image does nothing.  Specific commands are
# declared in docker-compose.yml via the ``command`` field.
CMD ["bash"]
