"""Redrive dead-lettered job(s) back into the retry lane.

    python manage.py redrive <job_id> [<job_id> ...]

Resets each DEAD_LETTER job to PENDING (fresh attempts, due now); the `recover_jobs`
requeue scan re-dispatches it. No new dispatch path.
"""

from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError

from jobs.services import redrive_dead_letter


class Command(BaseCommand):
    help = "Redrive DEAD_LETTER job(s) back into the retry lane."

    def add_arguments(self, parser):
        parser.add_argument("job_ids", nargs="+", help="Job UUID(s) to redrive.")

    def handle(self, *args, **options):
        valid, invalid = [], []
        for job_id in options["job_ids"]:
            (valid if _is_uuid(job_id) else invalid).append(job_id)
        for bad in invalid:
            self.stderr.write(f"skipped {bad}: not a valid UUID")

        redriven = redrive_dead_letter(valid) if valid else 0
        if not redriven:
            raise CommandError("no DEAD_LETTER jobs redriven")
        self.stdout.write(self.style.SUCCESS(f"redriven {redriven} job(s)"))


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False
