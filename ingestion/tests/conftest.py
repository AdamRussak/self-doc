"""Shared pytest fixtures for the ingestion test suite.

`_stub_dns_resolver` is autouse and process-wide: it replaces
`socket.getaddrinfo` with a deterministic, offline stub for every test in
this suite, so nothing here makes a real DNS query. Test fixtures routinely
use realistic-looking hostnames (`example.com`, `widget.example.com`,
`docs.docker.com`, `appwrite.io`, `nextjs.org`, ...) purely as readable
stand-ins for "some source's docs site" — they are never meant to be
resolved for real, and `app.urlscope`'s SSRF guard (`_resolve_host_addrs`,
used by `url_host_is_private`) resolves the host of every URL it validates.
Without this stub, most of the crawler/config/admin test suite would
silently depend on outbound network access and real upstream DNS answering
for those names, which is slow, flaky offline, and not this repo's
convention (the crawler tests use `httpx.MockTransport` specifically to
keep the suite off the network).

The stub always answers with the SAME public, non-private IPv4 literal
(`93.184.216.34` — the well-known former example.com address; any public,
non-reserved address would do) regardless of the hostname asked for, which
is enough for every test that just needs "this host is NOT private" to
hold.

INTERACTION WITH TESTS THAT NEED A *PRIVATE* RESOLUTION: several tests
(`test_urlscope.py`, `test_config.py`, and
`test_crawler.py::test_crawl_refuses_unresolvable_host_fail_closed`)
deliberately exercise the private-resolution / unresolvable-host paths of
the SSRF guard and install their OWN
`monkeypatch.setattr(socket, "getaddrinfo", ...)` inside the test body.
Because pytest hands out exactly one `monkeypatch` fixture instance per
test (shared by every fixture and the test function itself that requests
it) and undoes its patches in LIFO order at teardown, a test's own
`setattr` simply shadows this stub for the remainder of that one test; at
teardown the test's own patch is undone first (reverting to this stub),
then this fixture's own patch is undone (reverting to the real
`socket.getaddrinfo`). Neither stub ever leaks into a different test.

`_resolve_host_addrs` is `lru_cache`d in `app.urlscope` specifically so
repeated lookups of the same host within one crawl don't re-resolve — but
across TESTS that cache is exactly the kind of shared, hidden state that
must never survive from one test into the next: a verdict cached by one
test (e.g. "example.com resolves to 93.184.216.34, therefore public") must
never silently satisfy a different test that expects a different
resolution for the same hostname (some of the tests above reuse hostnames
like `2130706433` / IP-literal-as-hostname strings across files). This
fixture clears that cache both before and after every test to guarantee
that isolation; without the clear, a private-resolution test running after
a public-resolution test for the same host could pass for the wrong
reason (stale cache), or a public-resolution test running after a
private-resolution one could wrongly refuse.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest
from app.urlscope import _resolve_host_addrs

# Any public, non-private/non-link-local/non-loopback/non-reserved address
# works here. NOTE: the IANA TEST-NET-1/2/3 documentation ranges
# (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) are NOT safe substitutes —
# Python's `ipaddress.IPv4Address.is_private` reports them as private, which
# would make every stubbed host look private and break every test that
# expects a public verdict. 93.184.216.34 (the historical example.com
# address) is a real, public, non-reserved IPv4 literal.
_STUB_PUBLIC_IP = "93.184.216.34"
_real_getaddrinfo = socket.getaddrinfo

# Hosts needed by FastEmbed / HuggingFace Hub / Qdrant when initializing or checking model files,
# plus localhost hostnames so local database/service connections resolve cleanly.
_ALLOW_REAL_DNS_HOSTS = (
    "huggingface.co",
    "hf.co",
    "github.com",
    "githubusercontent.com",
    "qdrant.to",
    "googleapis.com",
    "python.org",
    "pypi.org",
    "amazonaws.com",
    "cloudfront.net",
    "localhost",
)


def _stub_getaddrinfo(host, port=0, family=0, type=0, proto=0, flags=0):
    """Deterministic, offline replacement for `socket.getaddrinfo`.

    A REAL OS resolver never does a network round-trip for an IP literal
    (`socket.getaddrinfo("192.168.1.10", None)` returns that same address
    instantly, purely locally) — only a HOSTNAME lookup goes over the wire.
    Several tests pass a literal private/loopback/link-local IP address
    directly as the "host" specifically to assert the SSRF guard's
    literal-IP classification (`_addr_is_private`) without involving
    resolution at all; if this stub substituted a public address for those
    literals too, it would silently defeat that assertion. So: an IP
    literal is echoed back unchanged (matching real `getaddrinfo`
    semantics); anything else (an actual hostname) resolves to the fixed
    public `_STUB_PUBLIC_IP`, regardless of what hostname was asked for.
    """
    if host and any(host == d or host.endswith("." + d) for d in _ALLOW_REAL_DNS_HOSTS):
        return _real_getaddrinfo(host, port, family, type, proto, flags)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addr = _STUB_PUBLIC_IP
    else:
        addr = host
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port or 0))]


@pytest.fixture(autouse=True)
def _stub_dns_resolver(monkeypatch):
    _resolve_host_addrs.cache_clear()
    monkeypatch.setattr(socket, "getaddrinfo", _stub_getaddrinfo)
    yield
    _resolve_host_addrs.cache_clear()
