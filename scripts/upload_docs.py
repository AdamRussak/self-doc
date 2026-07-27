#!/usr/bin/env python3
"""Standalone CLI script to upload local files into an existing self-docs
`source_type='upload'` doc source.

Walks one or more file/directory PATH arguments, batches the collected
files to respect the ingestion server's request-body cap
(`MaxBodySizeMiddleware`, a hard 50 MiB limit — see ingestion/app/main.py),
authenticates with `SYNC_TOKEN` against `/admin/login` (the same
session-cookie + CSRF flow scripts/push_sources.py already implements),
resolves `--source NAME` to the source's numeric id by parsing the rendered
`/admin` HTML (there is no JSON listing endpoint), and submits each batch
via `POST /admin/sources/{id}/upload` (multipart, one or more `files`
parts per request).

Usage:
    python3 scripts/upload_docs.py --source my-docs ./some/docs/dir file.pdf \
        [--url http://127.0.0.1:8080] [--token "$SYNC_TOKEN"] [--continue-on-error]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import sys
from pathlib import Path

try:
    import httpx
except ImportError:
    print("FATAL: httpx package is required. Please install it or run within the ingestion/.venv environment.", file=sys.stderr)
    sys.exit(1)

# Suffixes this CLI treats as "uploadable" when walking a directory on disk.
# Mirrors ingestion/app/uploads.py's ALLOWED_SUFFIXES PLUS '.zip': that
# constant only covers suffixes dispatched through the uploads.PARSERS
# registry, and (per T5) deliberately excludes '.zip' even though zip
# bundles ARE supported server-side (expanded via upload_zip.expand_zip
# ahead of the PARSERS registry, in ingestion/app/admin.py's upload route).
# This script decides what's uploadable by walking the filesystem, not by
# dispatching through PARSERS, so it needs its own explicit list.
ALLOWED_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".zip"}

# Stay comfortably under the server's hard 50 MiB request-body cap
# (uploads.MAX_UPLOAD_BYTES, enforced by MaxBodySizeMiddleware).
MAX_BATCH_FILES = 20
MAX_BATCH_BYTES = 40 * 1024 * 1024  # 40 MB

# (absolute path on disk, relative filename to send as the multipart upload name)
FileEntry = tuple[Path, str]

_ERROR_DIV_PATTERN = re.compile(r'<div class="error">(.*?)</div>', re.DOTALL)
_WHITESPACE_PATTERN = re.compile(r"\s+")


def compute_csrf_token(sync_token: str) -> str:
    """Derive the deterministic HMAC-SHA256 CSRF token expected by admin.py.

    Copied verbatim from scripts/push_sources.py — do not reimplement.
    """
    return hmac.new(sync_token.encode("utf-8"), b"csrf-v1", hashlib.sha256).hexdigest()


def collect_files(paths: list[Path]) -> tuple[list[FileEntry], list[str]]:
    """Resolve PATH positionals into a flat list of files to upload.

    A directory is walked recursively (`Path.rglob`) and every file whose
    suffix (case-insensitive) is in ALLOWED_SUFFIXES is collected, with its
    upload filename set to its path relative to that directory (preserving
    subdirectory structure in the `rel_path` the server derives). A single
    file PATH is used directly regardless of extension — let the server
    reject it if unsupported rather than second-guessing the user here.

    Returns (entries, warnings); warnings note paths that don't exist.
    """
    entries: list[FileEntry] = []
    warnings: list[str] = []
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file() and candidate.suffix.lower() in ALLOWED_SUFFIXES:
                    rel_name = candidate.relative_to(path).as_posix()
                    entries.append((candidate, rel_name))
        elif path.is_file():
            entries.append((path, path.name))
        else:
            warnings.append(f"Path not found, skipping: {path}")
    return entries, warnings


def batch_files(
    entries: list[FileEntry],
    max_files: int = MAX_BATCH_FILES,
    max_bytes: int = MAX_BATCH_BYTES,
) -> list[list[FileEntry]]:
    """Group files into batches respecting the server's request-body cap.

    At most `max_files` files OR `max_bytes` total bytes per batch. A
    single file that alone exceeds `max_bytes` is sent alone in its own
    batch rather than skipped.
    """
    batches: list[list[FileEntry]] = []
    current: list[FileEntry] = []
    current_bytes = 0

    for entry in entries:
        size = entry[0].stat().st_size
        if current and (len(current) >= max_files or current_bytes + size > max_bytes):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(entry)
        current_bytes += size
        if size > max_bytes and len(current) == 1:
            # Oversized single file: ships alone rather than blocking/skipping.
            batches.append(current)
            current = []
            current_bytes = 0

    if current:
        batches.append(current)

    return batches


def resolve_source_id(html: str, name: str) -> int | None:
    """Resolve a source NAME to its numeric id by parsing the rendered
    `/admin` HTML page.

    There is no JSON listing endpoint for doc_sources (per T9/T10's
    dependency contract) — push_sources.py already establishes the pattern
    of driving the admin UI's own HTML forms rather than inventing new
    server-side surface area, so this mirrors that by scraping the
    server-rendered page instead.

    Checks the "sync target" `<select>` on the /admin index first (this is
    where active sources — including active upload-type sources — are
    listed as `<option value="{id}">{name} ({base_url})</option>`), then
    falls back to matching the name against its `/admin/sources/{id}` edit
    link (covers sources still in `pending` review). `doc_sources.name` is
    unique, so the first match for either pattern is unambiguous.
    """
    escaped = re.escape(name)
    option_match = re.search(rf'<option value="(\d+)">{escaped}\s*\(', html)
    if option_match:
        return int(option_match.group(1))
    row_match = re.search(rf'<strong[^>]*>{escaped}</strong>.*?href="/admin/sources/(\d+)"', html, re.DOTALL)
    if row_match:
        return int(row_match.group(1))
    return None


def extract_error_message(resp: "httpx.Response") -> str:
    """Best-effort extraction of a human-readable error from a non-2xx/303
    admin response — never surfaces a raw traceback or full HTML dump."""
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = resp.json()
            if isinstance(data, dict) and "detail" in data:
                return str(data["detail"])
        except Exception:
            pass
    match = _ERROR_DIV_PATTERN.search(resp.text)
    if match:
        return _WHITESPACE_PATTERN.sub(" ", match.group(1)).strip()
    return resp.text[:200]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload local files (Markdown/text, HTML, PDF, zip bundles) into an existing self-docs source_type='upload' doc source."
    )
    parser.add_argument("--source", required=True, help="Name of the existing source_type='upload' doc source to upload into.")
    parser.add_argument("paths", nargs="+", type=Path, metavar="PATH", help="File(s) and/or directory(ies) to upload. Directories are walked recursively for allowed suffixes.")
    parser.add_argument("-u", "--url", default="http://127.0.0.1:8080", help="Base URL of the ingestion server (default: http://127.0.0.1:8080).")
    parser.add_argument("-t", "--token", default=None, help="SYNC_TOKEN value. If omitted, read from SYNC_TOKEN env var.")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep uploading remaining batches after a failure instead of aborting immediately.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP request timeout in seconds (default: 30.0).")
    args = parser.parse_args()

    sync_token = args.token or os.environ.get("SYNC_TOKEN")
    if not sync_token:
        print("FATAL: SYNC_TOKEN must be provided via --token or SYNC_TOKEN environment variable.", file=sys.stderr)
        return 1

    entries, warnings = collect_files(args.paths)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    if not entries:
        print("FATAL: No uploadable files found among the given paths.", file=sys.stderr)
        return 1

    print(f"Found {len(entries)} file(s) to upload for source {args.source!r}.")

    batches = batch_files(entries)
    print(f"Split into {len(batches)} batch(es) (max {MAX_BATCH_FILES} files / {MAX_BATCH_BYTES // (1024 * 1024)} MB each).")

    csrf_token = compute_csrf_token(sync_token)
    base_url = args.url.rstrip("/")

    uploaded_count = 0
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

        # Step 2: Resolve --source NAME -> numeric id by scraping /admin
        print(f"Resolving source {args.source!r} to its numeric id...")
        try:
            admin_resp = client.get("/admin")
        except httpx.RequestError as exc:
            print(f"FATAL: Could not fetch {base_url}/admin to resolve source id: {exc}", file=sys.stderr)
            return 1
        if admin_resp.status_code != 200:
            print(f"FATAL: Could not fetch {base_url}/admin (HTTP {admin_resp.status_code}).", file=sys.stderr)
            return 1
        source_id = resolve_source_id(admin_resp.text, args.source)
        if source_id is None:
            print(f"FATAL: No source named {args.source!r} found on the server. Create it first via the admin UI.", file=sys.stderr)
            return 1
        print(f"Resolved source {args.source!r} -> id {source_id}.")

        # Step 3: Submit each batch to POST /admin/sources/{id}/upload
        for batch_index, batch in enumerate(batches):
            names = [name for _path, name in batch]
            print(f"[batch {batch_index + 1}/{len(batches)}] Uploading {len(batch)} file(s): {', '.join(names)}")

            file_handles = []
            try:
                multipart_files = []
                for path, name in batch:
                    fh = open(path, "rb")
                    file_handles.append(fh)
                    multipart_files.append(("files", (name, fh, "application/octet-stream")))
                resp = client.post(
                    f"/admin/sources/{source_id}/upload",
                    data={"csrf_token": csrf_token},
                    files=multipart_files,
                )
            except httpx.RequestError as exc:
                print(f"  -> FAILED: Network error uploading batch: {exc}", file=sys.stderr)
                for name in names:
                    print(f"     {name}: failed (network error: {exc})")
                failed_count += len(batch)
                if args.continue_on_error:
                    continue
                return 1
            finally:
                for fh in file_handles:
                    fh.close()

            if resp.status_code == 303:
                print(f"  -> SUCCESS: batch uploaded ({len(batch)} file(s)).")
                for name in names:
                    print(f"     {name}: uploaded")
                uploaded_count += len(batch)
                continue

            # Every other status is a batch-level failure. Extract a clean,
            # human-readable reason (never a raw traceback/HTML dump).
            reason = extract_error_message(resp)
            print(f"  -> FAILED: HTTP {resp.status_code}: {reason}", file=sys.stderr)
            for name in names:
                print(f"     {name}: failed ({reason})")
            failed_count += len(batch)
            if not args.continue_on_error:
                return 1

    print("\n=== Upload Summary ===")
    print(f"Total files : {len(entries)}")
    print(f"Uploaded    : {uploaded_count}")
    print(f"Failed      : {failed_count}")

    return 0 if (failed_count == 0) or args.continue_on_error else 1


if __name__ == "__main__":
    sys.exit(main())
