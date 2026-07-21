#!/usr/bin/env python3
"""Standalone CLI script to push full-schema JSON doc sources to self-docs.

Reads a JSON file containing a list of `SourceConfig` objects, authenticates
with `SYNC_TOKEN` against `/admin/login`, computes the synchronizer CSRF token,
transforms list fields (`include_prefixes`, `exclude_prefixes`) into newline-joined
strings, and submits each source via `POST /admin/sources/new`.

Usage:
    python3 scripts/push_sources.py --file sources.json [--url http://127.0.0.1:8080] [--token "$SYNC_TOKEN"] [--continue-on-error] [--sync-after]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    print("FATAL: httpx package is required. Please install it or run within the ingestion/.venv environment.", file=sys.stderr)
    sys.exit(1)

NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")


def compute_csrf_token(sync_token: str) -> str:
    """Derive the deterministic HMAC-SHA256 CSRF token expected by admin.py."""
    return hmac.new(sync_token.encode("utf-8"), b"csrf-v1", hashlib.sha256).hexdigest()


def validate_source_item(item: dict[str, Any], index: int) -> list[str]:
    """Perform pre-flight validation on a single JSON item before sending over HTTP."""
    errors: list[str] = []
    if not isinstance(item, dict):
        return [f"Item #{index + 1} is not a JSON object (dict)."]

    name = item.get("name")
    if not name or not isinstance(name, str) or not NAME_PATTERN.match(name):
        errors.append(f"Item #{index + 1}: 'name' must be a non-empty string matching ^[a-z0-9-]+$, got {name!r}.")

    base_url = item.get("base_url")
    if not base_url or not isinstance(base_url, str) or not (base_url.startswith("http://") or base_url.startswith("https://")):
        errors.append(f"Item #{index + 1} ({name or 'unknown'}): 'base_url' must be a valid http(s) URL, got {base_url!r}.")

    max_pages = item.get("max_pages")
    if max_pages is None or not isinstance(max_pages, int) or max_pages <= 0:
        errors.append(f"Item #{index + 1} ({name or 'unknown'}): 'max_pages' is required and must be a positive integer, got {max_pages!r}.")

    sitemap = item.get("sitemap")
    if sitemap and isinstance(sitemap, str) and base_url and isinstance(base_url, str):
        try:
            base_host = urlparse(base_url).netloc
            sitemap_host = urlparse(sitemap).netloc
            if base_host and sitemap_host and base_host != sitemap_host:
                errors.append(
                    f"Item #{index + 1} ({name or 'unknown'}): sitemap host {sitemap_host!r} differs from base_url host {base_host!r}."
                )
        except Exception:
            pass

    return errors


def prepare_form_data(item: dict[str, Any], csrf_token: str) -> dict[str, str]:
    """Convert JSON object fields to application/x-www-form-urlencoded strings."""
    def join_prefixes(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, list):
            return "\n".join(str(p) for p in val if p is not None)
        return str(val)

    return {
        "name": str(item["name"]).strip(),
        "base_url": str(item["base_url"]).strip(),
        "sitemap": str(item.get("sitemap") or "").strip(),
        "include_prefixes": join_prefixes(item.get("include_prefixes")),
        "exclude_prefixes": join_prefixes(item.get("exclude_prefixes")),
        "max_pages": str(item["max_pages"]),
        "language": str(item.get("language") or "english").strip().lower(),
        "rate_limit_rps": str(item.get("rate_limit_rps") if item.get("rate_limit_rps") is not None else "1.0"),
        "llms_txt": str(item.get("llms_txt") or "auto").strip().lower(),
        "csrf_token": csrf_token,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Automate pushing full-schema doc_source JSON objects to self-docs.")
    parser.add_argument("-f", "--file", required=True, type=Path, help="Path to JSON file containing list of sources.")
    parser.add_argument("-u", "--url", default="http://127.0.0.1:8080", help="Base URL of the ingestion server (default: http://127.0.0.1:8080).")
    parser.add_argument("-t", "--token", default=None, help="SYNC_TOKEN value. If omitted, read from SYNC_TOKEN env var.")
    parser.add_argument("--sync-after", action="store_true", help="Trigger an immediate sync for each source created.")
    parser.add_argument("--continue-on-error", action="store_true", help="Log warning and skip items that fail validation/posting instead of aborting.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP request timeout in seconds (default: 30.0).")
    args = parser.parse_args()

    sync_token = args.token or os.environ.get("SYNC_TOKEN")
    if not sync_token:
        print("FATAL: SYNC_TOKEN must be provided via --token or SYNC_TOKEN environment variable.", file=sys.stderr)
        return 1

    if not args.file.exists():
        print(f"FATAL: Input file not found: {args.file}", file=sys.stderr)
        return 1

    try:
        with open(args.file, encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:
        print(f"FATAL: Failed to read/parse JSON file {args.file}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print("FATAL: JSON root element must be a list of source objects (`[...]`).", file=sys.stderr)
        return 1

    print(f"Loaded {len(data)} source item(s) from {args.file}.")

    # Pre-flight validation
    all_errors: list[str] = []
    for i, item in enumerate(data):
        errs = validate_source_item(item, i)
        all_errors.extend(errs)

    if all_errors:
        print("Pre-flight validation failed:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        if not args.continue_on_error:
            return 1
        print("(--continue-on-error enabled: proceeding with valid items only)")

    csrf_token = compute_csrf_token(sync_token)
    base_url = args.url.rstrip("/")

    created_count = 0
    skipped_count = 0
    failed_count = 0

    with httpx.Client(base_url=base_url, timeout=args.timeout, follow_redirects=False) as client:
        # Step 1: Exchange SYNC_TOKEN for session cookie at /admin/login
        print(f"Authenticating with {base_url}/admin/login...")
        try:
            login_resp = client.post("/admin/login", data={"token": sync_token})
            session_val = client.cookies.get("admin_session")
            if login_resp.status_code not in (200, 302, 303) or not session_val:
                print(
                    f"FATAL: Authentication failed (HTTP {login_resp.status_code}). Check your SYNC_TOKEN or server status.",
                    file=sys.stderr,
                )
                return 1
            # Explicitly set Cookie header so httpx sends it even over unencrypted http:// (since Set-Cookie has Secure=True)
            client.headers["Cookie"] = f"admin_session={session_val}"
            print("Successfully authenticated and obtained session cookie.")
        except httpx.RequestError as exc:
            print(f"FATAL: Could not connect to ingestion server at {base_url}: {exc}", file=sys.stderr)
            return 1

        # Step 2: Loop and submit each source item
        for i, item in enumerate(data):
            item_errs = validate_source_item(item, i)
            if item_errs:
                skipped_count += 1
                continue

            name = item["name"].strip()
            form_payload = prepare_form_data(item, csrf_token)

            print(f"[{i+1}/{len(data)}] Pushing source {name!r}...")
            try:
                resp = client.post("/admin/sources/new", data=form_payload)
                if resp.status_code == 303:
                    print(f"  -> SUCCESS: Source {name!r} created active.")
                    created_count += 1

                    if args.sync_after:
                        # Trigger sync
                        print(f"  -> Triggering sync for {name!r}...")
                        sync_resp = client.post(
                            "/sync",
                            json={"source": name},
                            headers={"Authorization": f"Bearer {sync_token}"},
                        )
                        if sync_resp.status_code in (200, 202):
                            print(f"     Sync started (`{sync_resp.status_code}`).")
                        else:
                            print(f"     Warning: Sync trigger returned HTTP {sync_resp.status_code}: {sync_resp.text}")
                elif resp.status_code == 400:
                    # Try to extract human error message or print warning
                    msg = "Validation error returned by server (HTTP 400)."
                    if "already exists" in resp.text.lower():
                        msg = f"Source {name!r} already exists on server."
                    print(f"  -> FAILED: {msg}", file=sys.stderr)
                    if args.continue_on_error:
                        failed_count += 1
                    else:
                        return 1
                elif resp.status_code in (401, 403):
                    print(f"  -> FAILED: Authentication/CSRF error (HTTP {resp.status_code}). Session may have expired.", file=sys.stderr)
                    if args.continue_on_error:
                        failed_count += 1
                    else:
                        return 1
                else:
                    print(f"  -> FAILED: Unexpected HTTP status {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                    if args.continue_on_error:
                        failed_count += 1
                    else:
                        return 1
            except httpx.RequestError as exc:
                print(f"  -> FAILED: Network error pushing {name!r}: {exc}", file=sys.stderr)
                if args.continue_on_error:
                    failed_count += 1
                else:
                    return 1

    print("\n=== Push Summary ===")
    print(f"Total processed: {len(data)}")
    print(f"Created active : {created_count}")
    print(f"Skipped/Failed : {skipped_count + failed_count}")

    return 0 if (failed_count == 0 and skipped_count == 0) or args.continue_on_error else 1


if __name__ == "__main__":
    sys.exit(main())
