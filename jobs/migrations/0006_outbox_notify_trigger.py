"""Postgres LISTEN/NOTIFY push-dispatch: notify on every outbox insert.

An AFTER INSERT trigger on the outbox table calls ``pg_notify('foreman_outbox')``.
Because NOTIFY is transactional, the signal is delivered exactly when the Job +
OutboxEvent commit — the ``outbox_listener`` command wakes and dispatches
immediately instead of waiting for the next Beat poll (see ADR 0007). Beat stays
as the fallback, so a missed notification still heals on the next poll.

Guarded to PostgreSQL: SQLite (the fast local test path) has no plpgsql, and the
concurrency/latency behaviour is a Postgres-runtime property exercised in CI.
"""

from __future__ import annotations

from django.db import migrations

CHANNEL = "foreman_outbox"

FORWARD_SQL = f"""
CREATE OR REPLACE FUNCTION foreman_notify_outbox() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('{CHANNEL}', '');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER outbox_notify
    AFTER INSERT ON jobs_outboxevent
    FOR EACH STATEMENT EXECUTE FUNCTION foreman_notify_outbox();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS outbox_notify ON jobs_outboxevent;
DROP FUNCTION IF EXISTS foreman_notify_outbox();
"""


def create_trigger(apps, schema_editor):
    # plpgsql is Postgres-only; SQLite test runs skip the trigger (Beat poll still dispatches).
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD_SQL)


def drop_trigger(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0005_job_finished_at_job_started_at"),
    ]

    operations = [
        migrations.RunPython(create_trigger, drop_trigger),
    ]
