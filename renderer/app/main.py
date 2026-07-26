"""Headless-render microservice (T7) -- opt-in, `render` compose profile.

Single job: given a URL, load it in headless Chromium (Playwright) long
enough for client-side JS to paint, and return the resulting DOM's HTML.
Exists as a wholly SEPARATE container so the ~400 MiB resident Chromium
footprint (plus a much larger image) never touches the ingestion image —
ingestion runs at ~820 MiB of a 1.5 GiB limit today and has no headroom for
that. See docker-compose.yml's `renderer` service comment and
`ingestion/app/renderer.py` (the caller).

Defense in depth against becoming an open SSRF proxy (this container is
never published on a host port and is only reachable from other containers
on the internal compose network, but a caller bug or a compromised sibling
container must still not be able to abuse this as a blind fetch-any-url
proxy):

  - `RENDER_TOKEN`, if set, is required as the `X-Render-Token` header on
    every request.
  - Every requested URL's host is independently re-validated against
    private/loopback/link-local/reserved address space, resolving DNS and
    checking every resulting address (mirrors
    `ingestion/app/urlscope.py::url_host_is_private`'s intent; duplicated
    rather than imported because this is a genuinely separate deployable
    with no dependency on the ingestion package). This holds even though
    the ingestion caller has already validated the source's `base_url`
    against the exact same class of address at config-load time -- this is
    a second, independent gate a buggy or compromised caller cannot bypass.
  - Navigation is capped by an explicit HARD per-request timeout
    (`RENDER_TIMEOUT_S`), enforced with `asyncio.wait_for` around the WHOLE
    Playwright call (not just Playwright's own best-effort navigation
    timeout) -- a hung browser process can never hang a request past this
    ceiling.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from playwright.async_api import Browser, Playwright, async_playwright
from pydantic import BaseModel

RENDER_TOKEN = os.environ.get("RENDER_TOKEN", "").strip()
# Hard ceiling on the ENTIRE render call (browser context creation + page
# navigation + content extraction), enforced independently of Playwright's
# own navigation timeout below.
RENDER_TIMEOUT_S = float(os.environ.get("RENDER_TIMEOUT_S", "20"))
# Playwright's own navigation timeout -- deliberately shorter than
# RENDER_TIMEOUT_S so a slow page fails via Playwright's own clean timeout
# error first, in the common case, rather than via the outer wait_for cancel.
NAV_TIMEOUT_MS = int(float(os.environ.get("RENDER_NAV_TIMEOUT_S", "15")) * 1000)

app = FastAPI(title="self-docs-renderer")

_playwright: Playwright | None = None
_browser: Browser | None = None


class RenderRequest(BaseModel):
    url: str


class RenderResponse(BaseModel):
    html: str


def _host_is_private(url: str) -> bool:
    """True if `url`'s host IS or RESOLVES TO private/loopback/link-local/
    reserved/multicast address space, OR cannot be resolved at all.

    Fails CLOSED on an unresolvable host -- unlike ingestion's config-load-
    time check (which fails open so a transient DNS blip doesn't
    permanently reject a legitimate source), this is a hot, per-page,
    crawl-time gate: a host this process cannot resolve is a URL it cannot
    safely render, full stop.
    """
    hostname = urlparse(url).hostname
    if not hostname:
        return True

    try:
        ip = ipaddress.ip_address(hostname)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        pass  # not an IP literal -- resolve it below

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


@app.on_event("startup")
async def _startup() -> None:
    global _playwright, _browser
    _playwright = await async_playwright().start()
    # --no-sandbox: Chromium's setuid sandbox needs kernel features Docker
    # containers don't grant by default (no unprivileged user namespaces
    # without extra flags); the container boundary is the sandbox here.
    # --disable-dev-shm-usage: /dev/shm defaults to 64 MiB in Docker, too
    # small for Chromium's shared memory use -- avoids relying on a large
    # shm_size being configured correctly on every deployment.
    _browser = await _playwright.chromium.launch(
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _browser is not None:
        await _browser.close()
    if _playwright is not None:
        await _playwright.stop()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


async def _render(url: str) -> str:
    assert _browser is not None
    context = await _browser.new_context()
    try:
        page = await context.new_page()

        async def _block_heavy_resources(route):
            # Images/media/fonts don't affect extracted TEXT content and
            # meaningfully cut render time + memory for image-heavy doc
            # sites -- this is a render service, not a visual renderer.
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _block_heavy_resources)
        await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="networkidle")
        return await page.content()
    finally:
        await context.close()


@app.post("/render", response_model=RenderResponse)
async def render(req: RenderRequest, x_render_token: str | None = Header(default=None)):
    if RENDER_TOKEN and x_render_token != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing render token")

    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http(s)")
    if _host_is_private(req.url):
        raise HTTPException(status_code=400, detail="refusing to render a private/reserved-address host")

    try:
        html = await asyncio.wait_for(_render(req.url), timeout=RENDER_TIMEOUT_S)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail="render timed out") from e
    except Exception as e:  # noqa: BLE001 - surfaced as a 502 so the ingestion caller's soft-fail path triggers
        raise HTTPException(status_code=502, detail=f"render failed: {e}") from e

    return RenderResponse(html=html)
