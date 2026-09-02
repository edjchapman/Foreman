"""Wait for the database to accept connections, then migrate.

    python manage.py migrate_when_ready

The web service's Railway pre-deploy command. On a cold `terraform apply` every service
is created in the same second, and Railway publishes a service's `<name>.railway.internal`
DNS record only once that service has a *running* deployment — so a pre-deploy that
resolves Postgres immediately can lose the race and fail the whole deploy with
`Name or service not known`, leaving the domain with nothing behind it. Observed
2026-09-01: web's pre-deploy fired 28s before Postgres finished deploying.

One command rather than a `wait_for_db && migrate` pair because Railway's
`preDeployCommand` is not documented to run through a shell — `&&` may never be
interpreted, and a pre-deploy that silently never migrates is worse than the race it
replaces. Only connection errors are retried, so a genuinely broken migration still
fails on its first attempt rather than after the timeout.
"""

from __future__ import annotations

import time
from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import OperationalError, connections

# Sized above a cold-start Postgres: the observed fresh-apply gap was ~28s from service
# creation to a running deployment, and the image pull dominates that.
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_INTERVAL_SECONDS = 2.0


class Command(BaseCommand):
    help = "Wait for the database to accept connections, then run migrate."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_TIMEOUT_SECONDS,
            help="Seconds to keep retrying the connection "
            f"(default {DEFAULT_TIMEOUT_SECONDS:.0f}).",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=DEFAULT_INTERVAL_SECONDS,
            help=f"Seconds between attempts (default {DEFAULT_INTERVAL_SECONDS:.0f}).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        self._wait_for_database(options["timeout"], options["interval"])
        call_command("migrate", interactive=False, verbosity=options["verbosity"])

    def _wait_for_database(self, timeout: float, interval: float) -> None:
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            try:
                connections["default"].ensure_connection()
            except OperationalError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandError(
                        f"database unreachable after {timeout:.0f}s ({attempt} attempts): {exc}"
                    ) from exc
                # One line per attempt, on stderr: Railway's deploy log is the only place
                # this is ever read, and a silent wait looks identical to a hung deploy.
                self.stderr.write(f"database not ready (attempt {attempt}): {exc}")
                time.sleep(min(interval, remaining))
            else:
                if attempt > 1:
                    self.stdout.write(
                        self.style.SUCCESS(f"database ready after {attempt} attempts")
                    )
                return
