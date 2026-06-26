import pytest
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _eager_celery(settings):
    """Run Celery tasks inline so the suite needs no broker.

    With ALWAYS_EAGER, `.delay()` executes synchronously in-process against the
    test DB — enough to exercise the outbox relay and worker end-to-end. CI then
    needs only Postgres (which `select_for_update(skip_locked=True)` requires),
    no Redis.
    """
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.fixture
def api_client():
    return APIClient()
