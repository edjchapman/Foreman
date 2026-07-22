"""Remote https:// CSV sources: SSRF guard, limits, and the failure taxonomy.

All transport is faked (opener + DNS monkeypatched) — no live network, ever.
The taxonomy matters most: poison inputs must raise an IngestError subclass
(worker fails fast), transient network failures must NOT (worker retries).
"""

import socket
import urllib.error
from email.message import Message

import pytest

from jobs import sources
from jobs.ingest import IngestError, load_csv_text
from jobs.models import Job, PropertyRecord
from jobs.sources import RemoteSourceError, TransientSourceError, fetch_remote_csv
from jobs.tests.factories import JobFactory

SAMPLE_CSV = load_csv_text({"source": "sample:properties.csv"})

# Hosts the fake resolver knows; documentation/TEST-NET ranges are deliberately
# avoided for the public entries (ipaddress treats them as non-global too).
PUBLIC_HOSTS = {"csv.example.com": "93.184.216.34", "cdn.example.com": "151.101.1.140"}
BLOCKED_HOSTS = {
    "internal.example.com": "10.0.0.5",
    "localhost": "127.0.0.1",
    "link.example.com": "169.254.1.1",
}


def _headers(content_type: str = "text/csv", location: str | None = None) -> Message:
    headers = Message()
    headers["Content-Type"] = content_type
    if location:
        headers["Location"] = location
    return headers


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/csv"):
        self._remaining = body
        self.headers = _headers(content_type)

    def read(self, size: int) -> bytes:
        chunk, self._remaining = self._remaining[:size], self._remaining[size:]
        return chunk

    def close(self) -> None:
        pass


class FakeOpener:
    """Maps URL -> FakeResponse | Exception; records the URLs actually requested."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requested: list[str] = []

    def open(self, request, timeout):
        url = request.full_url
        self.requested.append(url)
        outcome = self.routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http_error(url: str, code: int, location: str | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "nope", _headers(location=location), None)


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    table = {**PUBLIC_HOSTS, **BLOCKED_HOSTS}

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "flaky-dns.example.com":
            raise socket.gaierror(socket.EAI_AGAIN, "try again")
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 443))]

    monkeypatch.setattr(sources.socket, "getaddrinfo", fake_getaddrinfo)


def _route(monkeypatch, routes: dict) -> FakeOpener:
    opener = FakeOpener(routes)
    monkeypatch.setattr(sources, "_OPENER", opener)
    return opener


# --- Happy path and limits ----------------------------------------------------------


def test_fetches_a_public_https_csv(monkeypatch):
    url = "https://csv.example.com/data.csv"
    _route(monkeypatch, {url: FakeResponse(SAMPLE_CSV.encode())})

    assert fetch_remote_csv(url) == SAMPLE_CSV


def test_utf8_bom_is_stripped(monkeypatch):
    url = "https://csv.example.com/bom.csv"
    _route(monkeypatch, {url: FakeResponse(b"\xef\xbb\xbf" + SAMPLE_CSV.encode())})

    assert fetch_remote_csv(url) == SAMPLE_CSV


def test_body_over_the_byte_cap_is_poison(monkeypatch, settings):
    settings.REMOTE_SOURCE_MAX_BYTES = 16
    url = "https://csv.example.com/huge.csv"
    _route(monkeypatch, {url: FakeResponse(b"x" * 64)})  # streamed cap, no Content-Length

    with pytest.raises(RemoteSourceError, match="exceeds 16 bytes"):
        fetch_remote_csv(url)


def test_non_csv_content_type_is_poison(monkeypatch):
    url = "https://csv.example.com/page"
    _route(monkeypatch, {url: FakeResponse(b"<html>", content_type="text/html")})

    with pytest.raises(RemoteSourceError, match="content type"):
        fetch_remote_csv(url)


def test_non_utf8_body_is_poison(monkeypatch):
    url = "https://csv.example.com/latin1.csv"
    _route(monkeypatch, {url: FakeResponse(b"\xff\xfe\xba\xad")})

    with pytest.raises(RemoteSourceError, match="not valid UTF-8"):
        fetch_remote_csv(url)


# --- Failure taxonomy: poison (IngestError) vs transient (retryable) -----------------


def test_client_error_is_poison(monkeypatch):
    url = "https://csv.example.com/missing.csv"
    _route(monkeypatch, {url: _http_error(url, 404)})

    with pytest.raises(IngestError):  # RemoteSourceError IS an IngestError → FAILED
        fetch_remote_csv(url)


@pytest.mark.parametrize("code", [429, 500, 503])
def test_server_pressure_is_transient(monkeypatch, code):
    url = "https://csv.example.com/busy.csv"
    _route(monkeypatch, {url: _http_error(url, code)})

    with pytest.raises(TransientSourceError):
        fetch_remote_csv(url)


def test_connection_failure_is_transient(monkeypatch):
    url = "https://csv.example.com/down.csv"
    _route(monkeypatch, {url: urllib.error.URLError(TimeoutError("timed out"))})

    with pytest.raises(TransientSourceError):
        fetch_remote_csv(url)


def test_unknown_host_is_poison():
    with pytest.raises(RemoteSourceError, match="cannot resolve"):
        fetch_remote_csv("https://no-such-host.example.com/x.csv")


def test_temporary_dns_failure_is_transient():
    with pytest.raises(TransientSourceError, match="temporary DNS"):
        fetch_remote_csv("https://flaky-dns.example.com/x.csv")


# --- SSRF guard -----------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["http", "ftp", "file"])
def test_non_https_schemes_are_rejected(scheme):
    with pytest.raises(RemoteSourceError, match="https only"):
        fetch_remote_csv(f"{scheme}://csv.example.com/data.csv")


@pytest.mark.parametrize("host", sorted(BLOCKED_HOSTS))
def test_hosts_resolving_to_non_public_addresses_are_blocked(host):
    with pytest.raises(RemoteSourceError, match="non-public"):
        fetch_remote_csv(f"https://{host}/data.csv")


def test_redirect_to_a_private_address_is_blocked(monkeypatch):
    url = "https://csv.example.com/redirect"
    _route(
        monkeypatch,
        {url: _http_error(url, 302, location="https://internal.example.com/loot.csv")},
    )

    with pytest.raises(RemoteSourceError, match="non-public"):
        fetch_remote_csv(url)


def test_redirect_to_a_public_host_is_followed_and_revalidated(monkeypatch):
    first = "https://csv.example.com/moved.csv"
    final = "https://cdn.example.com/data.csv"
    opener = _route(
        monkeypatch,
        {
            first: _http_error(first, 302, location=final),
            final: FakeResponse(SAMPLE_CSV.encode()),
        },
    )

    assert fetch_remote_csv(first) == SAMPLE_CSV
    assert opener.requested == [first, final]  # each hop validated then fetched


def test_endless_redirects_are_poison(monkeypatch):
    url = "https://csv.example.com/loop"
    _route(monkeypatch, {url: _http_error(url, 302, location=url)})

    with pytest.raises(RemoteSourceError, match="too many redirects"):
        fetch_remote_csv(url)


# --- Worker integration: the taxonomy drives the state machine -----------------------


@pytest.mark.django_db
def test_https_job_imports_end_to_end(monkeypatch):
    from jobs.tasks import process_job

    monkeypatch.setattr(sources, "fetch_remote_csv", lambda url: SAMPLE_CSV)
    job = JobFactory(payload={"source": "https://csv.example.com/data.csv"})

    assert process_job(str(job.id)) == "succeeded"
    assert PropertyRecord.objects.filter(job=job).count() == 5


@pytest.mark.django_db
def test_https_poison_fails_without_retry(monkeypatch):
    from jobs.tasks import process_job

    def raise_poison(url):
        raise RemoteSourceError("HTTP 404")

    monkeypatch.setattr(sources, "fetch_remote_csv", raise_poison)
    job = JobFactory(payload={"source": "https://csv.example.com/missing.csv"})

    assert process_job(str(job.id)) == "failed"
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED
    assert job.attempts == 1


@pytest.mark.django_db
def test_https_transient_failure_schedules_a_retry(monkeypatch):
    from jobs.tasks import process_job

    def raise_transient(url):
        raise TransientSourceError("timed out")

    monkeypatch.setattr(sources, "fetch_remote_csv", raise_transient)
    job = JobFactory(payload={"source": "https://csv.example.com/slow.csv"})

    assert process_job(str(job.id)) == "retry"
    job.refresh_from_db()
    assert job.status == Job.Status.PENDING
    assert job.available_at is not None  # backoff scheduled
