FROM frolvlad/alpine-python3

# System deps for building Python packages
RUN apk add --no-cache \
      gcc \
      musl-dev \
      libffi-dev \
      openssl-dev \
      python3-dev \
      tzdata \
      git \
 && python -m pip install --no-cache-dir --upgrade pip --break-system-packages

# Install Python deps (copied separately so image only rebuilds when deps change)
WORKDIR /tmp
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt --break-system-packages