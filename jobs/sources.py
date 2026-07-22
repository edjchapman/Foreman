"""Remote CSV sources — the ``https://`` half of the ingest seam (case study: "build next").

``fetch_remote_csv`` downloads a CSV over HTTPS with the guards a public,
unauthenticated demo needs:

- **Scheme allow-list** — ``https://`` only (no plain ``http://``).
- **SSRF guard** — the host must resolve to public addresses only
  (``ip_address(...).is_global``); redirects are followed manually (max
  ``MAX_REDIRECTS`` hops) and **every hop is re-validated**. Known boundary: a
  DNS-rebinding attacker who flips the record between this check and the socket
  connect can still reach a private address — pinning the connection to the
  resolved IP under TLS/SNI is out of proportion here, so the window is
  documented rather than closed (the project's usual habit).
- **Limits** — ``REMOTE_SOURCE_MAX_BYTES`` enforced on the *streamed* body
  (Content-Length is advisory, never trusted), ``REMOTE_SOURCE_TIMEOUT_SECONDS``
  per request, and a CSV-ish content-type allow-list.

Failure taxonomy (mirrors ``jobs/faults.py``'s trick): inputs that can never
succeed — bad scheme/URL, blocked host, 4xx, over-size, wrong content type —
raise :class:`RemoteSourceError` (an ``IngestError``), so the worker fails the
job fast as poison. Plausibly-recoverable failures — DNS blips, timeouts,
connection/TLS errors, 429, 5xx — raise :class:`TransientSourceError`
(deliberately *not* an ``IngestError``), flowing into the retry/backoff branch.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlparse

from django.conf import settings

from .ingest import IngestError

ALLOWED_CONTENT_TYPES = ("text/csv", "text/plain", "application/octet-stream")
MAX_REDIRECTS = 3
_CHUNK_BYTES = 64 * 1024
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class RemoteSourceError(IngestError):
    """Poison remote source — permanent; the worker fails the job without retrying."""


class TransientSourceError(Exception):
    """Retryable fetch failure — deliberately not an IngestError, so the worker retries."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn 3xx into HTTPError so each hop can be re-validated before following."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_remote_csv(url: str) -> str:
    """Fetch ``url`` (https only) and return its body as text, within the guards above."""
    for _ in range(MAX_REDIRECTS + 1):
        _validate(url)
        redirect = _fetch_or_redirect(url)
        if isinstance(redirect, str):
            url = redirect
            continue
        return redirect[0]
    raise RemoteSourceError(f"too many redirects (> {MAX_REDIRECTS})")


def _fetch_or_redirect(url: str) -> tuple[str] | str:
    """One request: return ``(body,)`` on success or the next URL on a redirect."""
    request = urllib.request.Request(url, headers={"Accept": "text/csv"})  # noqa: S310 — scheme+host validated in _validate
    try:
        response = _OPENER.open(request, timeout=settings.REMOTE_SOURCE_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        if exc.code in _REDIRECT_CODES:
            location = exc.headers.get("Location")
            if not location:
                raise RemoteSourceError(f"redirect from {url!r} without a Location") from exc
            return urljoin(url, location)
        if exc.code == 429 or exc.code >= 500:
            raise TransientSourceError(f"HTTP {exc.code} from {url!r}") from exc
        raise RemoteSourceError(f"HTTP {exc.code} from {url!r}") from exc
    except urllib.error.URLError as exc:  # timeout, refused, TLS — all retryable
        raise TransientSourceError(f"fetch failed for {url!r}: {exc.reason}") from exc
    try:
        content_type = response.headers.get_content_type()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise RemoteSourceError(f"unsupported content type {content_type!r} from {url!r}")
        return (_read_capped(response, url).decode("utf-8-sig"),)
    except UnicodeDecodeError as exc:
        raise RemoteSourceError(f"remote CSV from {url!r} is not valid UTF-8") from exc
    finally:
        response.close()


def _read_capped(response: Any, url: str) -> bytes:
    """Stream the body, aborting past the byte cap — Content-Length is never trusted."""
    limit = settings.REMOTE_SOURCE_MAX_BYTES
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise RemoteSourceError(f"remote CSV from {url!r} exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RemoteSourceError(f"unsupported scheme {parsed.scheme!r} — https only")
    host = parsed.hostname
    if not host:
        raise RemoteSourceError(f"URL has no host: {url!r}")
    _reject_non_public(host)


def _reject_non_public(host: str) -> None:
    """SSRF guard: every address the host resolves to must be globally routable."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_AGAIN:  # resolver blip — worth a retry
            raise TransientSourceError(f"temporary DNS failure for {host!r}") from exc
        raise RemoteSourceError(f"cannot resolve host {host!r}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise RemoteSourceError(f"{host!r} resolves to a non-public address ({address})")
