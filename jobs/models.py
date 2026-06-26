import uuid

from django.db import models


class Job(models.Model):
    """A unit of asynchronous work.

    M1 only ever creates jobs in PENDING (there is no worker yet). The non-PENDING
    states and the `idempotency_key` / `attempts` fields are present from the start so
    the M2 outbox/worker and M3 reliability work don't churn the schema.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        DEAD_LETTER = "DEAD_LETTER", "Dead-letter"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job_type = models.CharField(max_length=64, default="property_csv_import")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    payload = models.JSONField(default=dict)
    # Unique-or-absent: a NULL means "no key supplied"; an empty string would collide
    # on the unique constraint, so null=True is the correct pattern here.
    idempotency_key = models.CharField(  # noqa: DJ001
        max_length=255,
        null=True,
        blank=True,
        unique=True,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Job {self.id} [{self.status}]"


class OutboxEvent(models.Model):
    """Transactional outbox row, written in the same DB txn as the Job it describes.

    The relay (`jobs.dispatch_outbox`) polls PENDING rows, publishes each to the
    broker, then marks it DISPATCHED. Because the Job and its OutboxEvent commit
    atomically, we never publish a message for a job that didn't persist, and never
    persist a job whose event was lost. Delivery is at-least-once (a crash between
    publish and the relay's commit re-sends); worker-side dedupe is M3.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DISPATCHED = "DISPATCHED", "Dispatched"

    id = models.BigAutoField(primary_key=True)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="outbox_events")
    event_type = models.CharField(max_length=64, default="job.created")
    # Snapshot of the message body at write time, so the relay is a dumb publisher
    # that never re-reads (and so never races) the Job.
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        # Partial index on the relay's hot path: stays small as DISPATCHED grows.
        indexes = [
            models.Index(
                fields=["id"],
                condition=models.Q(status="PENDING"),
                name="outbox_pending_idx",
            ),
        ]

    def __str__(self):
        return f"OutboxEvent {self.id} [{self.status}] {self.event_type}"
