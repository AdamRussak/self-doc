"""Client for the optional headless-render microservice (T7).

The renderer is a SEPARATE compose service (`renderer/`, `render` profile —
see `docker-compose.yml`) precisely so Chromium/Playwright never enters the
ingestion image: ingestion runs at ~820 MiB of a 1.5 GiB limit today, and
Chromium adds ~400 MiB resident plus a much larger image, which would risk an
OOM this service does not currently have. This module is therefore a thin,
dependency-free (no new packages — just `httpx`, already a dependency) HTTP
client, never a browser driver.

Every call here is SOFT-FAIL: an unreachable/slow/erroring renderer returns
`None`, exactly like a normal fetch failure, and never raises. This is the
single hard requirement of T7 — a hung/slow renderer must degrade to today's
soft-fail behavior and must never make a ~3.5 hour sync unbounded. Both an
explicit client-side timeout (`RENDERER_CLIENT_TIMEOUT_S`, enforced by
httpx) and the renderer's own server-side timeout guard against that; this
client only ever assumes the WORSE of the two actually held.

Callers (`store.sync_source`) are responsible for honoring the source's
`rate_limit_rps` around calls to `render_page` — this module does not rate
limit on its own, so it can be reused for a single ad-hoc call as well as a
rate-limited retry loop.
"""

from __future__ import annotations

import os

import httpx

from .logging_config import get_logger

logger = get_logger(component="renderer_client")

# Service-DNS name of the `renderer` compose service — reachable only over
# the internal compose network (no published host port either side), so this
# default only ever resolves inside the compose network, never from a host
# shell.
RENDERER_URL = os.environ.get("RENDERER_URL", "http://renderer:8090").rstrip("/")
RENDERER_TOKEN = os.environ.get("RENDER_TOKEN", "").strip()

# Hard ceiling on a single render call. This is deliberately generous (well
# above a typical page-render time) but still finite and short relative to a
# sync that can run ~3.5 hours: one hung render call must cost at most this
# many seconds, never hang forever waiting on a stuck browser process.
RENDERER_CLIENT_TIMEOUT_S = float(os.environ.get("RENDERER_CLIENT_TIMEOUT_S", "25"))


def render_page(url: str) -> str | None:
    """POST `{"url": url}` to the renderer's `/render` endpoint, returning
    the rendered page's HTML on success or `None` on ANY failure
    (unreachable, timeout, non-2xx, malformed body) — callers treat `None`
    exactly like a normal fetch failure. Never raises.
    """
    log = logger.bind(url=url)
    headers = {}
    if RENDERER_TOKEN:
        headers["X-Render-Token"] = RENDERER_TOKEN

    try:
        resp = httpx.post(
            f"{RENDERER_URL}/render",
            json={"url": url},
            headers=headers,
            timeout=RENDERER_CLIENT_TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        # Covers connect refused (profile disabled / renderer down),
        # DNS failure, and the client-side timeout firing.
        log.warning("renderer_unreachable_or_timed_out", error=str(e))
        return None

    if resp.status_code != 200:
        log.warning("renderer_error_response", status_code=resp.status_code, body=resp.text[:200])
        return None

    try:
        data = resp.json()
        html = data["html"]
    except Exception as e:  # noqa: BLE001 - a malformed response is a soft-fail, not a crash
        log.warning("renderer_malformed_response", error=str(e))
        return None

    if not isinstance(html, str) or not html:
        return None
    return html
