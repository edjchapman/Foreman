from django.urls import path
from rest_framework.routers import DefaultRouter

from .metrics import metrics_summary_view
from .views import JobViewSet

router = DefaultRouter()
router.register("jobs", JobViewSet, basename="job")

urlpatterns = [
    # JSON queue snapshot for the demo UI (the Prometheus text form stays at /metrics).
    path("metrics/summary", metrics_summary_view, name="metrics-summary"),
    *router.urls,
]
