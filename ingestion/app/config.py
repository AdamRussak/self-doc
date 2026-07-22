"""Pydantic models + loader for `sources.yaml`.

Schema (per IMPLEMENTATION_PLAN.md §2 `sources.yaml` schema):

    sources:
      - name: fastapi              # unique, [a-z0-9-], maps to doc_sources.name
        base_url: https://fastapi.tiangolo.com/
        sitemap: https://fastapi.tiangolo.com/sitemap.xml   # optional
        include_prefixes: ["/tutorial/", "/reference/"]      # optional allowlist
        exclude_prefixes: ["/blog/", "/release-notes/"]      # optional denylist (wins)
        max_pages: 500              # optional — omit for no page limit (crawl all in-scope pages)
        language: english           # optional, default english
        rate_limit_rps: 1.0         # optional, default 1.0

Validation fails fast (raises `ConfigError`) on:
  - duplicate `name` values
  - missing/invalid `base_url` (must be a valid http(s) URL)
  - unknown keys on a source entry
  - `name` not matching `^[a-z0-9-]+$`
  - a sitemap-less source whose `base_url` path is excluded by its own
    `include_prefixes`/`exclude_prefixes` (the BFS crawl seed would never
    pass its own filters, so the source would index 0 pages)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator, model_validator

from .urlscope import url_host_is_private

NAME_PATTERN = r"^[a-z0-9-]+$"

# Postgres built-in text-search configuration names (see `\dF` / pg_catalog
# `pg_ts_config` in a default install). `language` must be one of these,
# lowercased, since it is passed straight through to `to_tsvector(language, ...)`
# / `to_tsquery(language, ...)` — an invalid name errors at query time, not at
# config-load time, so we validate it up front instead.
SUPPORTED_FTS_LANGUAGES = frozenset(
    {
        "simple",
        "arabic",
        "armenian",
        "basque",
        "catalan",
        "danish",
        "dutch",
        "english",
        "finnish",
        "french",
        "german",
        "greek",
        "hindi",
        "hungarian",
        "indonesian",
        "irish",
        "italian",
        "lithuanian",
        "nepali",
        "norwegian",
        "portuguese",
        "romanian",
        "russian",
        "serbian",
        "spanish",
        "swedish",
        "tamil",
        "turkish",
        "yiddish",
    }
)


class ConfigError(ValueError):
    """Raised when sources.yaml fails validation. Message is human-readable."""


def _path_allowed(path: str, include_prefixes: list[str], exclude_prefixes: list[str]) -> bool:
    """Mirrors crawler._allowed: exclude_prefixes always wins over
    include_prefixes; an empty include_prefixes allowlists everything."""
    if any(path.startswith(p) for p in exclude_prefixes):
        return False
    if include_prefixes:
        return any(path.startswith(p) for p in include_prefixes)
    return True


class SourceConfig(BaseModel):
    """A single doc-source entry from sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    base_url: HttpUrl
    sitemap: HttpUrl | None = None
    include_prefixes: list[str] = Field(default_factory=list)
    exclude_prefixes: list[str] = Field(default_factory=list)
    max_pages: int | None = Field(default=None, gt=0)
    language: str = "english"
    rate_limit_rps: float = Field(default=1.0, gt=0)
    llms_txt: Literal["auto", "off", "only"] = "auto"

    @field_validator("include_prefixes", "exclude_prefixes", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return v if v is not None else []

    @field_validator("language", mode="after")
    @classmethod
    def _language_must_be_supported(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in SUPPORTED_FTS_LANGUAGES:
            raise ValueError(
                f"language {v!r} is not a supported Postgres text-search configuration "
                f"— must be one of SUPPORTED_FTS_LANGUAGES: {sorted(SUPPORTED_FTS_LANGUAGES)}"
            )
        return normalized

    @model_validator(mode="after")
    def _sitemap_shares_base_url_host(self) -> SourceConfig:
        # SSRF guard (security review H1): `sitemap` is fetched BEFORE any of
        # its `<loc>` entries are host-filtered, and a `<sitemapindex>` fans
        # out to its children equally unvalidated. Constraining the sitemap to
        # base_url's host closes both: the root request is in-scope by
        # construction and every child is checkable against the same host.
        # This is a real constraint on every real doc site.
        if self.sitemap is None:
            return self
        base_host = urlparse(str(self.base_url)).netloc
        sitemap_host = urlparse(str(self.sitemap)).netloc
        if sitemap_host != base_host:
            raise ValueError(
                f"source '{self.name}': sitemap host {sitemap_host!r} differs from base_url "
                f"host {base_host!r} — a sitemap is fetched before its entries are "
                "host-filtered, so an off-host sitemap is a server-side request forgery "
                "vector. Point the sitemap at base_url's own host."
            )
        return self

    @model_validator(mode="after")
    def _hosts_must_not_be_private(self) -> SourceConfig:
        # SSRF guard (security review H2): source URLs are untrusted input
        # (admin web form + an MCP tool callable by an AI agent), so reject a
        # host that IS or RESOLVES TO private/loopback/link-local/reserved
        # space at validation time — before a human is ever shown an approval
        # prompt. Fails closed on an unresolvable host. See
        # `urlscope._resolve_is_private` for the accepted DNS-rebinding
        # residual.
        for field_name, value in (("base_url", self.base_url), ("sitemap", self.sitemap)):
            if value is None:
                continue
            # unresolvable_is_private=False: validation must not permanently
            # reject a source because DNS blipped or the host does not resolve
            # from this machine. The crawl-time gate fails closed instead.
            if url_host_is_private(str(value), unresolvable_is_private=False):
                raise ValueError(
                    f"source '{self.name}': {field_name} host "
                    f"{urlparse(str(value)).hostname!r} is, resolves to, or cannot be "
                    "resolved away from a private/loopback/link-local/reserved address — "
                    "refusing to crawl internal network space."
                )
        return self

    @model_validator(mode="after")
    def _base_url_passes_own_prefix_filters(self) -> SourceConfig:
        # Without a sitemap, the crawler seeds its BFS queue with base_url
        # itself; if base_url's path is excluded (or not included) by this
        # source's own include/exclude_prefixes, the seed is filtered out
        # before the first fetch and the source silently indexes nothing
        # (see security/lesson: nextjs base_url `/docs` vs include_prefixes
        # `["/docs/"]`). Fail fast at config-load time instead.
        if self.sitemap is not None:
            return self
        path = urlparse(str(self.base_url)).path or "/"
        if not _path_allowed(path, self.include_prefixes, self.exclude_prefixes):
            raise ValueError(
                f"source '{self.name}': base_url path {path!r} is excluded by its own "
                f"include_prefixes={self.include_prefixes!r} / exclude_prefixes="
                f"{self.exclude_prefixes!r} — the BFS crawl seed would be filtered out "
                "before the first fetch, so this source would index 0 pages. Fix the "
                "prefixes so base_url's path itself is allowed."
            )
        return self


class SourcesFile(BaseModel):
    """Top-level `sources.yaml` document: {sources: [SourceConfig, ...]}."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceConfig]

    @field_validator("sources")
    @classmethod
    def _unique_names(cls, sources: list[SourceConfig]) -> list[SourceConfig]:
        seen: set[str] = set()
        dupes: set[str] = set()
        for s in sources:
            if s.name in seen:
                dupes.add(s.name)
            seen.add(s.name)
        if dupes:
            raise ValueError(f"duplicate source name(s): {sorted(dupes)}")
        return sources


def load_sources(path: str | Path) -> list[SourceConfig]:
    """Load and validate sources.yaml, raising ConfigError with a clear message
    on any schema violation (duplicate names, bad URLs, unknown keys, etc.).
    """
    path = Path(path)
    try:
        raw_text = path.read_text()
    except OSError as e:
        raise ConfigError(f"could not read sources file {path}: {e}") from e

    try:
        raw: dict[str, Any] = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e

    try:
        parsed = SourcesFile.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid sources.yaml ({path}): {e}") from e

    return parsed.sources
