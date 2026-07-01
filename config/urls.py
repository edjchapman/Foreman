from django.contrib import admin
from django.urls import include, path

from jobs.metrics import metrics_view
from jobs.views import DemoView, HealthView, ReadinessView

urlpatterns = [
    path("", DemoView.as_view(), name="demo"),
    path("admin/", admin.site.urls),
    path("healthz", HealthView.as_view(), name="healthz"),
    path("readyz", ReadinessView.as_view(), name="readyz"),
    path("metrics", metrics_view, name="metrics"),
    path("api/v1/", include("jobs.urls")),
]
