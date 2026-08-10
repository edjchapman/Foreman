"""LISTEN/NOTIFY push-dispatch: the outbox insert trigger and the listener command.

The NOTIFY-delivery test is Postgres-only and needs real commits, so it runs with
``transaction=True`` (a rolled-back test transaction would never fire the trigger's
on-commit notification). See ADR 0007 and migration 0006.
"""

import psycopg
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections

from jobs.management.commands import outbox_listener as listener_mod
from jobs.management.commands.outbox_listener import CHANNEL, Command, _conninfo
from jobs.models import OutboxEvent
from jobs.services import submit_job


def test_conninfo_is_built_from_django_settings():
    class FakeDB:
        settings_dict = {
            "NAME": "foreman",
            "USER": "foreman",
            "PASSWORD": "secret",
            "HOST": "db",
            "PORT": 5432,
        }

    info = _conninfo(FakeDB())
    assert "dbname=foreman" in info
    assert "host=db" in info
    assert "port=5432" in info


def test_listener_requires_postgres(monkeypatch):
    # The command refuses to run where LISTEN/NOTIFY isn't available.
    monkeypatch.setattr(connections["default"], "vendor", "sqlite", raising=False)
    with pytest.raises(CommandError, match="PostgreSQL"):
        call_command("outbox_listener")


def test_handle_connects_listens_and_sweeps(monkeypatch):
    """handle() opens a LISTEN connection, runs the initial sweep, then enters the loop.

    The blocking notify loop and the real connection are the integration boundary
    (exercised by the NOTIFY test + `make listener`); here we assert the wiring only.
    """
    monkeypatch.setattr(connections["default"], "vendor", "postgresql", raising=False)

    executed: list[str] = []

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            executed.append(sql)

    dispatched: list[int] = []
    monkeypatch.setattr(listener_mod.psycopg, "connect", lambda *a, **k: FakeConn())
    monkeypatch.setattr(listener_mod, "dispatch_outbox", lambda: dispatched.append(1))
    monkeypatch.setattr(Command, "_listen", lambda self, conn: None)  # don't block on I/O
    monkeypatch.setattr(listener_mod.signal, "signal", lambda *a, **k: None)  # no real handlers

    call_command("outbox_listener")

    assert any(f"LISTEN {CHANNEL}" in sql for sql in executed)
    assert dispatched == [1]  # the startup sweep dispatched once


def test_request_stop_sets_the_flag():
    cmd = Command()
    cmd._stop = False
    cmd._request_stop()
    assert cmd._stop is True


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="NOTIFY delivery is a Postgres runtime property"
)
@pytest.mark.django_db(transaction=True)
def test_submitting_a_job_emits_an_outbox_notify():
    """The AFTER INSERT trigger fires a NOTIFY on the channel the listener waits on."""
    listener = psycopg.connect(_conninfo(connections["default"]), autocommit=True)
    try:
        listener.execute(f"LISTEN {CHANNEL}")

        job, created = submit_job(
            job_type="property_csv_import",
            payload={"source": "sample:properties.csv"},
            idempotency_key=None,
        )
        assert created
        assert OutboxEvent.objects.filter(job=job).exists()

        # The commit above should have delivered exactly one notification on our channel.
        received = list(listener.notifies(timeout=5.0, stop_after=1))
        assert [n.channel for n in received] == [CHANNEL]
    finally:
        listener.close()
