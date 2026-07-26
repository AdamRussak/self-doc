# Base image ships Chromium (+ its OS deps) already installed and matched to
# the pinned `playwright` version below -- this is exactly the ~400 MiB+
# footprint T7 deliberately keeps OUT of the ingestion image, by putting it
# in this wholly separate service/container instead.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Non-root runtime user: reuse the base image's own `pwuser` (uid 1000)
# rather than creating a new one -- the Playwright image already grants it
# whatever access it needs to launch the bundled Chromium (browsers live
# under PLAYWRIGHT_BROWSERS_PATH, owned/readable for this exact purpose).
RUN chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
