from typing import Any

from django.conf import settings
from django.db import connection
from django.http import StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from config import celery_app

from .models import Job
from .realtime import notify_job
from .reports import report_filename, stream_report
from .serializers import JobCreateSerializer, JobSerializer
from .services import redrive_dead_letter, submit_job


class JobViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """Submit (POST), retrieve, and list jobs.

    Submitting records the job as PENDING and, in the same transaction, an outbox
    event the relay publishes to the worker (see `jobs.services.submit_job`). An
    optional `Idempotency-Key` header makes resubmission safe at the API edge;
    worker-side exactly-once *effect* comes from the idempotent re-import (see ADR 0002).
    """

    queryset = Job.objects.all()

    def get_serializer_class(self) -> type[serializers.BaseSerializer]:
        return JobCreateSerializer if self.action == "create" else JobSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        job, created = submit_job(
            idempotency_key=request.headers.get("Idempotency-Key"),
            **serializer.validated_data,
        )
        data = JobSerializer(job, context=self.get_serializer_context()).data
        if not created:
            return Response(data, status=status.HTTP_200_OK)
        location = reverse("job-detail", args=[job.id], request=request)
        return Response(data, status=status.HTTP_202_ACCEPTED, headers={"Location": location})

    @action(detail=True, methods=["get"])
    def report(self, request: Request, pk: str | None = None) -> HttpResponseBase:
        """Download the imported records as CSV — only available once SUCCEEDED.

        Streams via an async generator (`jobs.reports.stream_report`): under ASGI a
        sync iterator would make Django buffer the whole body. 409 (not 404) for a
        job that exists but hasn't succeeded — the resource is real, its state isn't
        ready (PENDING/PROCESSING) or never will be (FAILED/DEAD_LETTER).
        """
        job = self.get_object()
        if job.status != Job.Status.SUCCEEDED:
            return Response(
                {"detail": "Report is only available for succeeded jobs.", "status": job.status},
                status=status.HTTP_409_CONFLICT,
            )
        response = StreamingHttpResponse(stream_report(job), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{report_filename(job)}"'
        return response

    @action(detail=True, methods=["post"])
    def redrive(self, request: Request, pk: str | None = None) -> Response:
        """Redrive a dead-letter job back into the pipeline (the operator recovery action).

        Resets the job to PENDING with a fresh retry budget (see
        `jobs.services.redrive_dead_letter`); the `recover_jobs` scan re-dispatches it —
        no new dispatch path. 409 (not 404) for a job that exists but isn't DEAD_LETTER:
        the resource is real, its state just isn't redrivable.
        """
        job = self.get_object()
        if job.status != Job.Status.DEAD_LETTER:
            return Response(
                {"detail": "Only dead-letter jobs can be redriven.", "status": job.status},
                status=status.HTTP_409_CONFLICT,
            )
        redrive_dead_letter([job.id])
        job.refresh_from_db()
        # Broadcast the DEAD_LETTER → PENDING flip so any open status socket updates at once.
        notify_job(job)
        data = JobSerializer(job, context=self.get_serializer_context()).data
        return Response(data, status=status.HTTP_200_OK)


def check_database() -> bool:
    """True if the database answers a trivial query."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 — any DB error means not-ready
        return False
    return True


def check_broker() -> bool:
    """True if the Celery broker (Redis) accepts a connection."""
    conn = celery_app.connection()
    try:
        conn.ensure_connection(max_retries=1, timeout=2)
    except Exception:  # noqa: BLE001 — any broker error means not-ready
        return False
    finally:
        conn.release()
    return True


class HealthView(APIView):
    """Liveness probe: 200 while the process can serve requests.

    Deliberately does no dependency I/O — a DB or broker blip must not make an
    orchestrator restart the pod. Confirming dependencies is readiness's job
    (`ReadinessView`); conflating the two causes restart storms. See ADR 0003.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})


class ReadinessView(APIView):
    """Readiness probe: 200 only if the database and broker are both reachable.

    A dependency outage returns 503 — "stop routing traffic here", not "restart me".
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request: Request) -> Response:
        checks = {
            "database": "ok" if check_database() else "down",
            "broker": "ok" if check_broker() else "down",
        }
        ready = all(value == "ok" for value in checks.values())
        code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
        body = {"status": "ready" if ready else "not ready", "checks": checks}
        return Response(body, status=code)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class DemoView(TemplateView):
    """Minimal live job-status demo page — inline vanilla-JS WebSocket client.

    `ensure_csrf_cookie` sets the CSRF cookie on GET so the page's fetch can echo it
    back (robust even against a stray authenticated /admin session); the page itself is
    a pure client of the existing REST + WebSocket API. See the M4 demo-page slice.
    """

    template_name = "jobs/demo.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Single source of truth for the dead-letter demo's heal window (jobs/faults.py).
        context["heal_after_seconds"] = settings.DEMO_HEAL_AFTER_SECONDS
        return context
