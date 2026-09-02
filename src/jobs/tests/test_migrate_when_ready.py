"""The pre-deploy command rides out a cold private network without masking real failures."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connections

pytestmark = pytest.mark.django_db

MODULE = "jobs.management.commands.migrate_when_ready"


@pytest.fixture
def migrate_calls(monkeypatch):
    """Capture the delegated `migrate` instead of re-running it against the test DB."""
    calls = []
    monkeypatch.setattr(f"{MODULE}.call_command", lambda *a, **kw: calls.append((a, kw)))
    return calls


def _run(**kwargs):
    out, err = StringIO(), StringIO()
    call_command("migrate_when_ready", stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def _fail_n_times(monkeypatch, failures: float) -> list[int]:
    """Make the first `failures` connection attempts raise, then let them succeed.

    `float("inf")` means every attempt fails — a finite count is not "never arrives":
    with `interval=0` the wait loop spins tens of thousands of times inside a short
    timeout and would sail past any plausible integer.
    """
    attempts = []

    def fake_ensure_connection() -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) <= failures:
            raise OperationalError("failed to resolve host 'postgres.railway.internal'")

    monkeypatch.setattr(connections["default"], "ensure_connection", fake_ensure_connection)
    return attempts


def test_migrates_immediately_when_the_database_is_reachable(migrate_calls):
    out, err = _run()

    assert migrate_calls == [(("migrate",), {"interactive": False, "verbosity": 1})]
    assert err == ""  # no retry noise on the happy path
    assert "database ready" not in out


def test_retries_until_the_database_accepts_connections(monkeypatch, migrate_calls):
    attempts = _fail_n_times(monkeypatch, failures=2)

    out, err = _run(timeout=5, interval=0)

    assert len(attempts) == 3
    assert len(migrate_calls) == 1
    assert err.count("database not ready") == 2
    assert "database ready after 3 attempts" in out


def test_fails_without_migrating_when_the_database_never_arrives(monkeypatch, migrate_calls):
    _fail_n_times(monkeypatch, failures=float("inf"))

    with pytest.raises(CommandError, match="database unreachable"):
        _run(timeout=0.05, interval=0)

    assert migrate_calls == []  # never migrates against a database it cannot reach


def test_a_broken_migration_fails_on_the_first_attempt(monkeypatch):
    """Only connectivity is retried — a bad migration must not be retried for the timeout."""
    calls = []

    def exploding_migrate(*args, **kwargs):
        calls.append(args)
        raise CommandError("boom: inconsistent migration history")

    monkeypatch.setattr(f"{MODULE}.call_command", exploding_migrate)

    with pytest.raises(CommandError, match="boom"):
        _run(timeout=30, interval=0)

    assert len(calls) == 1
