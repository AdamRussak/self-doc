"""Boot-time shared-secret policy: refuse to serve a placeholder token to a
network-reachable listener.

WHY THIS EXISTS
---------------
`SYNC_TOKEN` is a single shared bearer secret that guards, on one service:

  - `POST /sync`, `POST /refresh`  (expensive: triggers crawls)
  - `POST /purge`                  (DESTRUCTIVE: deletes indexed pages)
  - the entire `/admin` UI login   (full CRUD over `doc_sources`, incl. delete)

`.env.example` historically shipped `SYNC_TOKEN=change-me`, and an operator
who copies the example and never edits it gets a deployment whose destructive
admin surface is protected by a secret that is published in this repository.
On the base `docker-compose.yml` that is survivable — both services publish on
`127.0.0.1` only, so nothing is reachable off-box. It stops being survivable
the moment a listener is exposed to the LAN (see `docker-compose.prod.yml`,
which attaches BOTH `mcp-server` and `ingestion` to an external Traefik
network).

So the policy is deliberately two-branch, and NOT "always refuse":

  loopback-only + placeholder token -> WARN loudly, keep booting.
      A local-only dev/home box with the shipped default is the status quo
      for every existing user of this repo. Turning that into a hard boot
      failure on upgrade would break working setups to fix a risk that the
      loopback bind is already containing.

  any non-loopback listener + placeholder token -> REFUSE to boot.
      Here the shipped default is a published credential on a reachable
      destructive API. Refusing at import time means the process dies before
      uvicorn binds a socket, so there is never a window in which the
      placeholder is actually accepted over the network.

HOW EXPOSURE IS DETERMINED
--------------------------
The application cannot introspect its own exposure: inside the container
uvicorn always binds `0.0.0.0:8080`, and whether that is reachable depends on
the compose port publishing (`127.0.0.1:8080:8080`) and on Traefik labels —
neither of which is visible from inside the process. So the compose layer,
which *does* know, declares it via `SELF_DOCS_LISTENERS`: a comma-separated
list of the addresses this deployment is actually reachable on.

  docker-compose.yml       SELF_DOCS_LISTENERS=127.0.0.1:8080      (loopback)
  docker-compose.prod.yml  SELF_DOCS_LISTENERS=...,https://${DOCS_MCP_HOSTNAME}/api/v1

UNSET means "assume loopback-only". That is the permissive default on
purpose: it keeps existing `.env` files, `make test`, and the test suite
working unchanged. It also means the refusal branch is opt-in via the prod
overlay — an operator who hand-rolls their own reverse proxy without setting
`SELF_DOCS_LISTENERS` still gets only the warning. That residual is documented
in `docs/runbook.md`; making the default fail-closed would refuse to boot for
every current user of the base compose file.

This module holds NO secret values and never logs a token. Messages name the
*variable*, never the value.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

# --- Known placeholder / shipped-default secrets ----------------------------------------
#
# Matched after `_normalize` (strip, unquote, casefold, `_`/space -> `-`), so
# `Change_Me`, `"CHANGE ME"` and `change-me` all collapse to one entry. This is
# an EXACT-match list plus the prefix rules in `_PLACEHOLDER_PREFIXES` below —
# deliberately not a length/entropy heuristic. A short-but-real token
# (`test-token-123` in this repo's own suite) must not be misclassified as a
# shipped default; the cost of a false refusal at boot is an outage, and the
# threat being defended against here is specifically "the operator never
# changed the value we published in git".
_PLACEHOLDER_TOKENS = frozenset(
    {
        "",
        "-",
        "0",
        "1",
        "123456",
        "abc123",
        "admin",
        "change-it",
        "change-me",
        "changeit",
        "change-this",
        "changeme",
        "changethis",
        "default",
        "dev",
        "dev-token",
        "example",
        "foo",
        "hunter2",
        "insecure",
        "letmein",
        "mcp-token",
        "me",
        "none",
        "null",
        "password",
        "password123",
        "placeholder",
        "replace-me",
        "replaceme",
        "secret",
        "super-secret",
        "supersecret",
        "sync-token",
        "tbd",
        "todo",
        "token",
        "unset",
        "xxx",
        "xxxxx",
    }
)

# Prefix rules catch the documented example shapes without needing every
# variant enumerated: `REPLACE_ME_WITH_A_GENERATED_TOKEN` (what `.env.example`
# now ships), `your-token-here`, `<your-token>`, etc.
_PLACEHOLDER_PREFIXES = (
    "change-me",
    "changeme",
    "replace-me",
    "replaceme",
    "your-",
    "<",
    "put-your-",
    "generate-",
)

# Hostnames that mean "this machine only" without needing to be resolved.
# Anything that is not one of these and is not a loopback IP literal is
# treated as non-loopback (fail-closed on the exposure side of the decision:
# an unrecognised host is assumed reachable).
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})

#: Fallback when `SELF_DOCS_LISTENERS` is unset — see the module docstring.
DEFAULT_LISTENERS: tuple[str, ...] = ("127.0.0.1",)

#: Env var the compose layer uses to declare real reachability.
LISTENERS_ENV_VAR = "SELF_DOCS_LISTENERS"


def _normalize(token: str) -> str:
    """Collapse cosmetic variation so one list entry covers the family.

    Strips whitespace and one layer of surrounding quotes (operators paste
    `SYNC_TOKEN="change-me"` into `.env` and docker-compose passes the quotes
    straight through in some shells), casefolds, and unifies `_`/space to `-`.
    """
    t = token.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        t = t[1:-1].strip()
    return t.casefold().replace("_", "-").replace(" ", "-")


def is_placeholder_token(token: str | None) -> bool:
    """True if `token` is a known shipped-default / example placeholder.

    Never logs or returns the value itself.
    """
    if token is None:
        return True
    normalized = _normalize(token)
    if normalized in _PLACEHOLDER_TOKENS:
        return True
    return any(normalized.startswith(p) for p in _PLACEHOLDER_PREFIXES)


def _host_of(listener: str) -> str:
    """Extract the host from a listener spec.

    Accepted shapes (all of which appear in this repo's compose files or in a
    plausible hand-rolled deployment):

        127.0.0.1:8080          host:port
        0.0.0.0:8080            host:port
        [::1]:8080              bracketed IPv6 + port
        ::1                     bare IPv6
        https://docs.example/x  URL
        docs.example.lan        bare hostname
    """
    spec = listener.strip()
    if "://" in spec:
        return (urlsplit(spec).hostname or "").strip()
    # Bracketed IPv6, with or without a port: [::1] / [::1]:8080
    if spec.startswith("["):
        end = spec.find("]")
        if end != -1:
            return spec[1:end]
    # A bare IPv6 literal has 2+ colons and no port; host:port has exactly one.
    if spec.count(":") == 1:
        return spec.rsplit(":", 1)[0]
    return spec


def is_loopback_listener(listener: str) -> bool:
    """True only if this listener is provably reachable from the local host
    alone. Unknown/unparseable specs are treated as NON-loopback so a typo in
    `SELF_DOCS_LISTENERS` errs toward the stricter branch."""
    host = _host_of(listener)
    if not host:
        return False
    if host.casefold() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A real hostname (docs-mcp.lan, a Traefik Host() rule, ...). Not
        # loopback, and we deliberately do NOT resolve it here: a boot-time
        # DNS lookup that answers "loopback" would let a DNS answer downgrade
        # a security decision.
        return False


def parse_listeners(raw: str | None) -> list[str]:
    """Split the `SELF_DOCS_LISTENERS` value; empty/unset -> the loopback default."""
    if raw is None:
        return list(DEFAULT_LISTENERS)
    items = [part.strip() for part in raw.split(",")]
    items = [part for part in items if part]
    return items or list(DEFAULT_LISTENERS)


@dataclass(frozen=True)
class TokenPolicyResult:
    """Outcome of the boot-time check. `message` is safe to print: it names
    the offending *variable* and the offending *listeners*, never the token."""

    verdict: str  # "ok" | "warn" | "refuse"
    message: str

    @property
    def should_refuse(self) -> bool:
        return self.verdict == "refuse"


_HOW_TO_FIX = (
    "Fix: generate a strong value with `openssl rand -hex 32`, set {var}=<that value> "
    "in .env (never commit .env), update every MCP/API client that sends "
    "`Authorization: Bearer ${var}` (see docs/client-setup.md), then "
    "`docker compose up -d ingestion`. Rotating {var} also invalidates every "
    "existing /admin session cookie. Full procedure: docs/runbook.md, "
    '"Rotate SYNC_TOKEN".'
)


def evaluate_token_policy(
    token: str | None,
    listeners: list[str],
    *,
    var_name: str = "SYNC_TOKEN",
) -> TokenPolicyResult:
    """Pure decision function — no I/O, no env reads, no exits.

    Returns "ok" for a non-placeholder token; "warn" for a placeholder behind
    loopback-only listeners; "refuse" for a placeholder with any non-loopback
    listener.
    """
    if not is_placeholder_token(token):
        return TokenPolicyResult("ok", "")

    exposed = [listener for listener in listeners if not is_loopback_listener(listener)]
    how = _HOW_TO_FIX.format(var=var_name)

    if exposed:
        return TokenPolicyResult(
            "refuse",
            f"FATAL: {var_name} is set to a known placeholder/shipped-default value "
            f"AND this deployment declares non-loopback listener(s): {', '.join(exposed)}. "
            f"That publishes a credential from this repository on a network-reachable API "
            f"that can trigger crawls, purge the index, and administer doc_sources. "
            f"Refusing to start. {how}",
        )

    return TokenPolicyResult(
        "warn",
        f"WARNING: {var_name} is set to a known placeholder/shipped-default value. "
        f"Every declared listener ({', '.join(listeners)}) is loopback-only, so this is "
        f"contained to this host and startup continues — but anyone with shell access to "
        f"this box, and anyone who later exposes this service (Traefik, a reverse proxy, "
        f"a published port, an SSH tunnel), owns the destructive /purge and /admin "
        f"surfaces. Startup will REFUSE, not warn, once a non-loopback listener is "
        f"declared in {LISTENERS_ENV_VAR}. {how}",
    )


def check_shared_token(
    token: str | None,
    env: Mapping[str, str] | None = None,
    *,
    var_name: str = "SYNC_TOKEN",
) -> TokenPolicyResult:
    """Env-reading wrapper over `evaluate_token_policy`. Still no printing and
    no exiting — the caller owns process control (see `app.main`)."""
    source: Mapping[str, str] = os.environ if env is None else env
    return evaluate_token_policy(token, parse_listeners(source.get(LISTENERS_ENV_VAR)), var_name=var_name)
