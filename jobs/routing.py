from django.urls import path

from .consumers import JobStatusConsumer

# The <uuid:job_id> converter validates the id at the router, so the consumer receives a
# UUID and a malformed path never reaches it.
websocket_urlpatterns = [
    path("ws/jobs/<uuid:job_id>/", JobStatusConsumer.as_asgi()),
]
