"""Zip-bundle expansion for the upload ingestion path (T5).

Registers a `.zip` parser into `uploads.PARSERS` so that
`uploads.parse_upload()` can dispatch zip archives the same way it dispatches
any other upload suffix. All zip handling in this module is in-memory only
(`io.BytesIO` over already-read bytes) — no code here writes to disk (the
ASGI multipart parser upstream may transiently spool a large uploaded part
to an OS temp file before this module ever receives its bytes —
`SpooledTemporaryFile`, framework-layer behavior, cleaned up automatically
at request end, not something this module does).

PARSERS-vs-ALLOWED_SUFFIXES: `uploads.parse_upload()` dispatches purely via
`uploads.PARSERS.get(suffix)` and never consults `uploads.ALLOWED_SUFFIXES`.
Registering `.zip` into `PARSERS` here is therefore sufficient to make zip
uploads reachable through `parse_upload()`; `ALLOWED_SUFFIXES` (which does
NOT list `.zip`) is left untouched per the task contract and is presumably
consumed elsewhere (e.g. a CLI walking a directory tree) rather than by the
parser dispatch path.

Security — every one of the following is checked from `zipfile.ZipInfo`
metadata (name, external_attr, file_size, compress_size) *before* any member
is decompressed via `.read()`/`.extract()`:
  (a) zip-slip: every member name must pass `uploads.normalize_rel_path`
  (b) symlink members are rejected (checked via the Unix mode bits packed
      into the upper 16 bits of `external_attr`)
  (c) archive member count must not exceed `uploads.MAX_ARCHIVE_MEMBERS`
  (d) total uncompressed size must not exceed `uploads.MAX_UNCOMPRESSED_BYTES`
  (e) any single member's expansion ratio must not exceed
      `uploads.MAX_EXPANSION_RATIO`

(a)-(d) are whole-archive checks: any violation raises `UploadError` for the
entire upload before any member is touched, and nothing is partially
ingested. (e) is a per-member check: a member that fails it (or whose own
parser raises `UploadError`, e.g. a malformed PDF inside the zip) is
recorded as a per-member failure and processing continues with the
remaining members — it does not abort the batch.

Nested `.zip` members (a zip inside a zip) are never recursed into; they are
skipped and counted. Members whose suffix has no registered parser are also
skipped and counted, silently (not an error).

Failure-reporting design (read this before consuming from T6/T9):
`register_parser` requires a callable matching
`Callable[[str, bytes], list[UploadedDoc]]` — exactly the same shape as
every other registered parser, and the shape `uploads.parse_upload()`
promises callers. To keep that contract intact, the callable actually
registered into `PARSERS` (`_parse_zip_for_registry`) returns *only* the
successfully-parsed docs; per-member failures and skips are reported as
structlog events (`upload_zip_member_failed` / `upload_zip_member_skipped`)
rather than returned, since a bare `list[UploadedDoc]` has nowhere else to
put them.

Callers that need the richer per-member detail — e.g. an admin UI or CLI
that wants to show "17 files imported, 2 failed: reason X" — should call
`expand_zip()` directly instead of going through `parse_upload()`. It
returns a `ZipExpansionResult` with `.docs` (successes), `.failures`
(`ZipMemberFailure(member, reason)` for real per-member errors), and
`.skipped` (member names skipped for expected, non-error reasons: nested
zips and unhandled suffixes).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from . import uploads
from .logging_config import get_logger
from .uploads import UploadedDoc, UploadError

logger = get_logger(component="upload_zip")

# Unix `S_IFLNK` (symlink) file-type bits, as packed into the upper 16 bits
# of ZipInfo.external_attr by zip tools running on POSIX systems.
_S_IFMT = 0o170000
_S_IFLNK = 0o120000

# Exceptions raised by zipfile.ZipFile.read() for a single malformed/
# encrypted member. Caught per-member so one bad entry doesn't abort the
# whole batch; anything outside this set is allowed to propagate.
_MEMBER_READ_ERRORS = (zipfile.BadZipFile, RuntimeError, OSError, EOFError)


@dataclass(frozen=True)
class ZipMemberFailure:
    """A single archive member that failed to parse.

    `reason` is a user-safe message (no stack traces / filesystem paths
    beyond the member's own upload-relative path, which the uploader
    already knows).
    """

    member: str
    reason: str


@dataclass(frozen=True)
class ZipExpansionResult:
    """Full result of expanding a zip archive.

    `docs`: successfully parsed documents, in archive order.
    `failures`: per-member errors (bad expansion ratio, unreadable member,
        or the member's own parser raising UploadError). The batch is not
        aborted because of these.
    `skipped`: member names skipped for expected, non-error reasons (nested
        `.zip` members, or members whose suffix has no registered parser).
    """

    docs: list[UploadedDoc]
    failures: list[ZipMemberFailure]
    skipped: list[str]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & _S_IFMT) == _S_IFLNK


def expand_zip(filename: str, data: bytes) -> ZipExpansionResult:
    """Expand an in-memory zip archive into parsed upload docs.

    Raises `UploadError` (whole-batch rejection, nothing partially
    ingested) if the archive is not a valid zip, or fails any of the
    whole-archive metadata checks (a)-(d) described in the module
    docstring. Per-member issues are collected into the returned
    `ZipExpansionResult.failures` / `.skipped` instead of raising.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadError("Uploaded file is not a valid zip archive.") from exc

    infolist = zf.infolist()

    # (c) member count - metadata only, no member touched.
    if len(infolist) > uploads.MAX_ARCHIVE_MEMBERS:
        raise UploadError(
            f"Zip archive contains too many files (max {uploads.MAX_ARCHIVE_MEMBERS})."
        )

    # (d) total uncompressed size - metadata only (file_size is stored in
    # the central directory; nothing is decompressed to compute this).
    total_uncompressed = sum(info.file_size for info in infolist)
    if total_uncompressed > uploads.MAX_UNCOMPRESSED_BYTES:
        raise UploadError("Zip archive is too large when uncompressed.")

    # (a) zip-slip and (b) symlink - checked for every member before any
    # member is read/decompressed. A violation on any single member rejects
    # the whole archive.
    normalized_names: dict[str, str] = {}
    for info in infolist:
        if info.is_dir():
            continue
        if _is_symlink(info):
            raise UploadError("Zip archive contains a symlink entry, which is not allowed.")
        # Raises UploadError (propagates, rejecting the whole batch) on
        # zip-slip / absolute / backslash paths - reuses uploads.py's
        # existing, already-tested logic rather than reimplementing it.
        normalized_names[info.filename] = uploads.normalize_rel_path(info.filename)

    docs: list[UploadedDoc] = []
    failures: list[ZipMemberFailure] = []
    skipped: list[str] = []

    for info in infolist:
        if info.is_dir():
            continue

        rel_path = normalized_names[info.filename]
        suffix = PurePosixPath(rel_path).suffix.lower()

        if suffix == ".zip":
            # Nested archive: skip and count, never recurse.
            skipped.append(rel_path)
            continue
        if suffix not in uploads.PARSERS:
            # No parser registered for this suffix: skip and count,
            # silently (not an error).
            skipped.append(rel_path)
            continue

        # (e) per-member expansion ratio - metadata only, no decompression
        # attempted for members that fail this check.
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > uploads.MAX_EXPANSION_RATIO:
            failures.append(
                ZipMemberFailure(
                    member=rel_path,
                    reason="This file's compressed size is suspiciously small for its "
                    "uncompressed size and was rejected.",
                )
            )
            continue

        try:
            member_bytes = zf.read(info)
        except _MEMBER_READ_ERRORS:
            failures.append(
                ZipMemberFailure(member=rel_path, reason="Could not read this file from the archive.")
            )
            continue

        try:
            docs.extend(uploads.parse_upload(rel_path, member_bytes))
        except UploadError as exc:
            failures.append(ZipMemberFailure(member=rel_path, reason=str(exc)))
            continue

    return ZipExpansionResult(docs=docs, failures=failures, skipped=skipped)


def _parse_zip_for_registry(filename: str, data: bytes) -> list[UploadedDoc]:
    """PARSERS-compatible wrapper around `expand_zip`.

    Returns only the successfully-parsed docs, matching the
    `Callable[[str, bytes], list[UploadedDoc]]` shape every registered
    parser (and `uploads.parse_upload()`) promises. Per-member failures and
    skips are logged as structlog warning/info events rather than raised or
    returned - see the module docstring's "Failure-reporting design"
    section. Callers that need the per-member detail should call
    `expand_zip()` directly.
    """
    result = expand_zip(filename, data)
    for failure in result.failures:
        logger.warning("upload_zip_member_failed", member=failure.member, reason=failure.reason)
    for skipped_member in result.skipped:
        logger.info("upload_zip_member_skipped", member=skipped_member)
    return result.docs


uploads.register_parser(".zip", _parse_zip_for_registry)
