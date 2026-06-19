FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg libavif-bin libgl1 libglib2.0-0 \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

# Exact runtime dependency set. requirements.txt points at the lock so local
# installs and Docker builds resolve the same versions.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt

# Package metadata
COPY pyproject.toml README.md ./

# App code
COPY src ./src
RUN pip install --no-cache-dir --no-build-isolation --no-deps . \
  && pip check

COPY config.yaml ./config.yaml
COPY healthcheck.sh ./healthcheck.sh

EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD sh /app/healthcheck.sh
ENTRYPOINT ["wanyard"]
CMD ["serve"]
