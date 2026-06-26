from django.db import connection
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

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
    worker-side exactly-once processing lands in M3.
    """

    queryset = Job.objects.all()

    def get_serializer_class(self):
        return JobCreateSerializer if self.action == "create" else JobSerializer

    def create(self, request, *args, **kwargs):
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


class HealthView(APIView):
    """Liveness probe that also confirms the database is reachable."""

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            return Response({"status": "unhealthy"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"status": "ok"})
