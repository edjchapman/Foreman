from collections.abc import Iterator

import pytest
from channels.layers import channel_layers
from pytest_django.fixtures import SettingsWrapper
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _eager_celery(settings: SettingsWrapper) -> None:
    """Run Celery tasks inline so the suite needs no broker.

    With ALWAYS_EAGER, `.delay()` executes synchronously in-process against the
    test DB — enough to exercise the outbox relay and worker end-to-end. CI then
    needs only Postgres (which `select_for_update(skip_locked=True)` requires),
    no Redis.
    """
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.fixture(autouse=True)
def _in_memory_channel_layer(settings: SettingsWrapper) -> Iterator[None]:
    """Route WebSocket fan-out through an in-process layer so the suite needs no Redis.

    `notify_job` fires from the task seams in every worker/reliability test; without this
    override those broadcasts would hit the real Redis layer. The manager caches backends
    per alias, so clear it for the override to take effect (and to isolate queues per test).
    """
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    channel_layers.backends.clear()
    yield
    channel_layers.backends.clear()


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()
