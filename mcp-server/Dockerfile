# self-docs mcp-server — FastMCP 3.x retrieval server (streamable HTTP, stateless)
FROM python:3.12-slim

# Avoid ONNX/OpenMP oversubscribing home-lab CPUs when both python services hold
# the FastEmbed model (see IMPLEMENTATION_PLAN.md §2 Operational standards).
ENV OMP_NUM_THREADS=1 \
    ORT_NUM_THREADS=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/home/app/.cache/fastembed

WORKDIR /app

# Install the package first so dependency layers cache independently of app code.
COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir .

# Non-root user; the FastEmbed cache lives under its home so the model can be
# pre-downloaded (as this user) at build time and re-used at runtime.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 app \
    && mkdir -p /home/app/.cache/fastembed \
    && chown -R app:app /home/app

USER app

# Pre-download the FastEmbed model at build time so the image works fully
# offline at runtime and the first request isn't paying a multi-second
# cold-load / download penalty (see IMPLEMENTATION_PLAN.md: "FastEmbed model
# cache — pre-baked into both images at build; runtime works offline").
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"

EXPOSE 8000

# python:3.12-slim has no curl; use urllib so the healthcheck needs no extra
# package. /metrics is a plain GET with no DB/model dependency, proving the
# HTTP listener itself is up (a full /mcp handshake would also touch the pool).
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=3)" || exit 1

CMD ["python", "-m", "app.server"]
