from typing import Any

from django.db import connection
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from config import celery_app

from .models import Job
from .serializers import JobCreateSerializer, JobSerializer
from .services import submit_job


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
