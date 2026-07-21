FROM python:3.12-slim

# ONNX Runtime (used by FastEmbed) shouldn't oversubscribe home-lab CPUs.
ENV OMP_NUM_THREADS=1 \
    ORT_NUM_THREADS=2 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: trafilatura/lxml need libxml2/libxslt at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY app ./app
COPY config ./config

# Install uv for lockfile-based reproducible builds, export requirements from lockfile, then install deps.
RUN pip install --no-cache-dir uv \
    && uv export --no-dev --no-editable --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt

# Pre-download the FastEmbed embedding model and its tokenizer at build time so
# the first real sync doesn't pay a cold-start download. Cached under
# /home/ingest/.cache, owned by the runtime user below. The model is a build
# arg so `make configure` / compose can bake the selected model (default =
# config/models.yaml registry default); the runtime env var must match it.
ARG EMBEDDING_MODEL_NAME=mixedbread-ai/mxbai-embed-large-v1
ENV EMBEDDING_MODEL_NAME=${EMBEDDING_MODEL_NAME}
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(model_name=os.environ['EMBEDDING_MODEL_NAME'])" \
    && python -c "import os; from tokenizers import Tokenizer; Tokenizer.from_pretrained(os.environ['EMBEDDING_MODEL_NAME'])"

# Non-root runtime user.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin ingest \
    && mkdir -p /home/ingest/.cache \
    && cp -r /root/.cache/. /home/ingest/.cache/ 2>/dev/null || true \
    && chown -R ingest:ingest /home/ingest /app

USER ingest
ENV HOME=/home/ingest

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
