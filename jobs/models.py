import uuid

from django.db import models


class Job(models.Model):
    """A unit of asynchronous work.

    M1 only ever creates jobs in PENDING (there is no worker yet). The non-PENDING
    states and the `idempotency_key` / `attempts` fields were front-loaded in M1 to
    minimise churn; M3 adds the lease/scheduling fields (`available_at`,
    `leased_until`, `lease_token`) that retries and crash-recovery need.
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
    idempotency_key = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    # M3 reliability state, all driven from Postgres (never the broker):
    # - available_at: when a (re)dispatch becomes eligible. NULL for a brand-new job
    #   (the outbox dispatches it); a future time schedules a backoff retry.
    # - leased_until / lease_token: the worker's lease while PROCESSING. The reaper
    #   reclaims an expired lease; the token fences a reclaimed-then-resumed worker's
    #   stale write so it cannot clobber the row.
    available_at = models.DateTimeField(null=True, blank=True)
    leased_until = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        # Partial index on the requeue scan's hot path (retry-scheduled rows only),
        # mirroring outbox_pending_idx — stays small as terminal rows accumulate.
        indexes = [
            models.Index(
                fields=["available_at"],
                condition=models.Q(status="PENDING", available_at__isnull=False),
                name="job_retry_due_idx",
            ),
        ]

    def __str__(self) -> str:
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

    def __str__(self) -> str:
        return f"OutboxEvent {self.id} [{self.status}] {self.event_type}"


class PropertyRecord(models.Model):
    """A single property row imported from a job's CSV.

    Exactly-once *effect* (M3): `(job, external_id)` is unique, so reprocessing a
    redelivered job converges on the same rows instead of duplicating them — the
    worker pairs this with `bulk_create(ignore_conflicts=True)`. Scoped per-job (not
    a global `external_id`) so distinct imports of the same property never collide.
    """

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="properties")
    external_id = models.CharField(max_length=64)
    address_line1 = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    postcode = models.CharField(max_length=16)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    bedrooms = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["job", "external_id"], name="uniq_property_per_job"),
        ]

    def __str__(self) -> str:
        return f"PropertyRecord {self.external_id} ({self.city})"
