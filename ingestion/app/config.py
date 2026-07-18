"""Pydantic models + loader for `sources.yaml`.

Schema (per IMPLEMENTATION_PLAN.md §2 `sources.yaml` schema):

    sources:
      - name: fastapi              # unique, [a-z0-9-], maps to doc_sources.name
        base_url: https://fastapi.tiangolo.com/
        sitemap: https://fastapi.tiangolo.com/sitemap.xml   # optional
        include_prefixes: ["/tutorial/", "/reference/"]      # optional allowlist
        exclude_prefixes: ["/blog/", "/release-notes/"]      # optional denylist (wins)
        max_pages: 500              # required
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
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator, model_validator

NAME_PATTERN = r"^[a-z0-9-]+$"


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
    max_pages: int = Field(gt=0)
    language: str = "english"
    rate_limit_rps: float = Field(default=1.0, gt=0)

    @field_validator("include_prefixes", "exclude_prefixes", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return v if v is not None else []

    @model_validator(mode="after")
    def _base_url_passes_own_prefix_filters(self) -> "SourceConfig":
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
