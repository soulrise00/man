# syntax=docker/dockerfile:1

############################
#  누끼 auto-registration  #
############################
FROM python:3.13-slim AS base

# onnxruntime needs libgomp; Pillow wheels are self-contained.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=4

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Bake the default model weights into the image so the first request does not
# wait on a download. Override at build time:  --build-arg REMBG_MODEL=birefnet-general
ARG REMBG_MODEL=isnet-general-use
ENV REMBG_MODEL=${REMBG_MODEL}
RUN python -c "from rembg import new_session; new_session('${REMBG_MODEL}'); print('baked', '${REMBG_MODEL}')"

# Runtime data (DB + stored assets) lives on a volume.
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
