import uuid
from typing import Self

from django.db import connection, models
from django.utils import timezone


class LockingQuerySet[M: models.Model](models.QuerySet[M]):
    """Row-locking helpers that degrade with the backend's capabilities.

    Postgres (production) takes real row locks; backends without ``FOR UPDATE``
    support (SQLite in local tests) fall back to a plain query — correct under the
    single-threaded suite; concurrency safety is a Postgres-runtime property
    exercised in CI.
    """

    def lock_for_claim(self) -> Self:
        """Lock rows for a claim, using SKIP LOCKED where the backend supports it.

        SKIP LOCKED lets parallel claimers (relays, workers, the reaper) take
        disjoint rows without blocking each other.
        """
        features = connection.features
        if not features.has_select_for_update:
            return self
        if features.has_select_for_update_skip_locked:
            return self.select_for_update(skip_locked=True)
        return self.select_for_update()

    def lock(self) -> Self:
        """Blocking row lock: concurrent callers wait rather than skip.

        For operations that must never silently under-count (e.g. redrive) —
        skipping a locked row would drop it from the batch.
        """
        if not connection.features.has_select_for_update:
            return self
        return self.select_for_update()


class Job(models.Model):
    """A unit of asynchronous work.

    Lifecycle: PENDING → PROCESSING → SUCCEEDED | FAILED | DEAD_LETTER — every
    transition lives in `jobs.lifecycle`, driven by `jobs.tasks.process_job`.
    The M3 lease/scheduling fields (`available_at`,
    `leased_until`, `lease_token`) carry retry backoff and crash-recovery state;
    `result` holds the import summary on success and `error` the failure detail.
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
    # Durable transition timestamps for latency metrics. `updated_at` is overwritten
    # on every save, so it can't reconstruct per-phase durations; these are stamped
    # once and never rewritten:
    # - started_at: first claim (PENDING→PROCESSING). queue_wait = started_at - created_at.
    # - finished_at: terminal transition. processing = finished_at - started_at.
    # Nullable/additive so the schema stays forward-compatible (no backfill needed).
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = LockingQuerySet.as_manager()

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

    objects = LockingQuerySet.as_manager()

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


class ProcessHeartbeat(models.Model):
    """A process's durable, periodically refreshed proof that it is making progress.

    Written after a successful work cycle — *progress*, not process-liveness: a
    wedged process stops beating even while its PID survives. Durable (a table
    row, not process state) because every metric is derived from Postgres at
    scrape time (see ``jobs.metrics``); the listener's silent death is otherwise
    masked by Beat's durability fallback and visible only as degraded latency.

    Rejected shapes: probing ``pg_stat_activity`` / ``application_name`` (proves
    the connection exists, not that work completes); exposing the raw timestamp
    (the endpoint's house style is age gauges); a boolean up-gauge (bakes the
    staleness threshold — alert policy — into app code).
    """

    name = models.CharField(max_length=64, primary_key=True)
    beat_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"ProcessHeartbeat {self.name} @ {self.beat_at}"

    @classmethod
    def beat(cls, name: str) -> None:
        """Record that ``name`` completed a work cycle just now (upsert)."""
        cls.objects.update_or_create(name=name, defaults={"beat_at": timezone.now()})
