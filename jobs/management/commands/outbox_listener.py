"""Push-dispatch the outbox on Postgres NOTIFY (see ADR 0007).

    python manage.py outbox_listener

Opens a dedicated connection, ``LISTEN``s on the ``foreman_outbox`` channel (fired by
the AFTER INSERT trigger from migration 0006), and dispatches the outbox the instant a
job is submitted — taking the Beat-poll latency out of the common path. Beat stays
scheduled as the fallback, so a missed notification (e.g. a listener restart) still
heals on the next poll and delivery remains at-least-once. The listener also sweeps on
a slow timer, so it is self-healing even if Beat is off.
"""

from __future__ import annotations

import signal
from typing import Any

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from jobs.tasks import dispatch_outbox

CHANNEL = "foreman_outbox"
# Wake at least this often even with no NOTIFY: a backstop sweep (independent of Beat)
# and the bound on how long a SIGTERM takes to stop the loop. The NOTIFY is the fast path.
SWEEP_INTERVAL_SECONDS = 5.0


class Command(BaseCommand):
    help = "LISTEN for outbox NOTIFYs and dispatch immediately (push dispatch)."

    def handle(self, *args: Any, **options: Any) -> None:
        db = connections["default"]
        if db.vendor != "postgresql":
            raise CommandError("outbox_listener requires PostgreSQL (LISTEN/NOTIFY).")

        self._stop = False
        signal.signal(signal.SIGINT, self._request_stop)
        signal.signal(signal.SIGTERM, self._request_stop)

        with psycopg.connect(_conninfo(db), autocommit=True) as conn:
            conn.execute(f"LISTEN {CHANNEL}")
            self.stdout.write(self.style.SUCCESS(f"listening on {CHANNEL}; Ctrl-C to stop"))
            dispatch_outbox()  # initial sweep: publish anything already pending at startup
            self._listen(conn)
        self.stdout.write("outbox_listener stopped")

    def _listen(self, conn: psycopg.Connection) -> None:  # pragma: no cover - blocking I/O loop
        while not self._stop:
            # Wake on the first notification (low latency) or after the sweep window;
            # dispatch_outbox claims the whole PENDING batch, so a burst coalesces.
            for _ in conn.notifies(timeout=SWEEP_INTERVAL_SECONDS, stop_after=1):
                pass
            dispatch_outbox()

    def _request_stop(self, *args: Any) -> None:
        self._stop = True


def _conninfo(db: Any) -> str:
    """Build a libpq conninfo string from Django's DB settings (a dedicated connection)."""
    s = db.settings_dict
    return psycopg.conninfo.make_conninfo(
        dbname=s.get("NAME") or "",
        user=s.get("USER") or None,
        password=s.get("PASSWORD") or None,
        host=s.get("HOST") or None,
        port=str(s["PORT"]) if s.get("PORT") else None,
    )
