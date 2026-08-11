"""ProcessHeartbeat: the durable progress artifact behind the dead-listener metric."""

from datetime import timedelta

import pytest
from django.utils import timezone

from jobs.models import ProcessHeartbeat

pytestmark = pytest.mark.django_db


def test_beat_creates_the_row_on_first_call():
    before = timezone.now()

    ProcessHeartbeat.beat("listener")

    row = ProcessHeartbeat.objects.get(name="listener")
    assert before <= row.beat_at <= timezone.now()


def test_beat_refreshes_the_existing_row():
    ProcessHeartbeat.objects.create(name="listener", beat_at=timezone.now() - timedelta(hours=1))

    ProcessHeartbeat.beat("listener")

    row = ProcessHeartbeat.objects.get(name="listener")  # still exactly one row (upsert)
    assert timezone.now() - row.beat_at < timedelta(seconds=5)
