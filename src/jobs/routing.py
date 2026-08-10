from django.urls import path

from .consumers import JobStatusConsumer, QueueConsumer

# The <uuid:job_id> converter validates the id at the router, so the consumer receives a
# UUID and a malformed path never reaches it. `ws/queue/` is the parameterless firehose the
# live board subscribes to.
websocket_urlpatterns = [
    path("ws/jobs/<uuid:job_id>/", JobStatusConsumer.as_asgi()),
    path("ws/queue/", QueueConsumer.as_asgi()),
]
