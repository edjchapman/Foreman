"""Liveness (`/healthz`), readiness (`/readyz`), and the dependency-check helpers.

The endpoint tests monkeypatch `check_database` / `check_broker`, so the helpers'
real bodies get their own unit tests (with a fake broker connection — the suite
has no live Redis). See ADR 0003 for the liveness/readiness split.
"""

import pytest

from jobs import views

pytestmark = pytest.mark.django_db


class _FakeConn:
    """Stand-in for a kombu broker connection."""

    def __init__(self, *, ok):
        self._ok = ok

    def ensure_connection(self, **kwargs):
        if not self._ok:
            raise ConnectionError("no broker")

    def release(self):
        pass


class _BrokenDB:
    def cursor(self):
        raise RuntimeError("db down")


# --- Liveness ---------------------------------------------------------------------


def test_healthz_ok(api_client):
    resp = api_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.data["status"] == "ok"


def test_healthz_is_liveness_only(api_client, monkeypatch):
    # Liveness does no DB I/O: even with the module's DB handle broken, it stays 200.
    # Guards against anyone re-adding a dependency check to the liveness probe.
    monkeypatch.setattr("jobs.views.connection", _BrokenDB())
    assert api_client.get("/healthz").status_code == 200


# --- Readiness --------------------------------------------------------------------


def test_readyz_ok(api_client, monkeypatch):
    monkeypatch.setattr("jobs.views.check_broker", lambda: True)  # no live Redis in tests
    resp = api_client.get("/readyz")
    assert resp.status_code == 200
    assert resp.data["status"] == "ready"
    assert resp.data["checks"] == {"database": "ok", "broker": "ok"}


def test_readyz_503_when_broker_down(api_client, monkeypatch):
    monkeypatch.setattr("jobs.views.check_broker", lambda: False)
    resp = api_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.data["checks"]["broker"] == "down"


def test_readyz_503_when_database_down(api_client, monkeypatch):
    monkeypatch.setattr("jobs.views.check_broker", lambda: True)
    monkeypatch.setattr("jobs.views.check_database", lambda: False)
    resp = api_client.get("/readyz")
    assert resp.status_code == 503
    assert resp.data["checks"]["database"] == "down"


# --- SSL-redirect exemption (platform healthchecks probe plain HTTP) ---------------


def test_health_endpoints_exempt_from_ssl_redirect(api_client, settings, monkeypatch):
    # Railway-style probes send plain HTTP with no forwarded-proto header; a 301
    # fails the probe, so both endpoints bypass SECURE_SSL_REDIRECT.
    settings.SECURE_SSL_REDIRECT = True
    monkeypatch.setattr("jobs.views.check_broker", lambda: True)
    assert api_client.get("/healthz").status_code == 200
    assert api_client.get("/readyz").status_code == 200


def test_ssl_redirect_still_covers_other_paths(api_client, settings):
    # Control: the exemption is scoped to the probes, not a redirect bypass.
    settings.SECURE_SSL_REDIRECT = True
    assert api_client.get("/").status_code == 301


# --- Dependency-check helpers (real bodies) ---------------------------------------


def test_check_database_true():
    assert views.check_database() is True  # the test DB is up


def test_check_database_false(monkeypatch):
    monkeypatch.setattr("jobs.views.connection", _BrokenDB())
    assert views.check_database() is False


def test_check_broker_true(monkeypatch):
    monkeypatch.setattr("jobs.views.celery_app.connection", lambda: _FakeConn(ok=True))
    assert views.check_broker() is True


def test_check_broker_false(monkeypatch):
    monkeypatch.setattr("jobs.views.celery_app.connection", lambda: _FakeConn(ok=False))
    assert views.check_broker() is False
