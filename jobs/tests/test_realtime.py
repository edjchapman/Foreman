"""Unit tests for the sync→async broadcast boundary (jobs.realtime.notify_job)."""

import logging

import pytest

from jobs.models import Job
from jobs.realtime import job_group, notify_job
from jobs.tests.factories import JobFactory

pytestmark = pytest.mark.django_db


class _RecordingLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group, message):
        self.sent.append((group, message))


class _BrokenLayer:
    async def group_send(self, group, message):
        raise RuntimeError("channel layer down")


def _use_layer(monkeypatch, layer):
    monkeypatch.setattr("jobs.realtime.get_channel_layer", lambda: layer)


def test_job_group_format():
    assert job_group("abc-123") == "job.abc-123"


def test_notify_job_addresses_group_with_serialized_state(monkeypatch):
    layer = _RecordingLayer()
    _use_layer(monkeypatch, layer)
    job = JobFactory()

    notify_job(job)

    assert len(layer.sent) == 1
    group, message = layer.sent[0]
    assert group == f"job.{job.id}"
    assert message["type"] == "job.update"
    assert message["data"]["id"] == str(job.id)
    assert message["data"]["status"] == "PENDING"
    assert "attempts" in message["data"]  # the field added for the live view


def test_notify_job_broadcasts_committed_not_stale_state(monkeypatch):
    layer = _RecordingLayer()
    _use_layer(monkeypatch, layer)
    job = JobFactory()  # in-memory instance stays PENDING / progress 0
    Job.objects.filter(pk=job.pk).update(status=Job.Status.SUCCEEDED, progress=100)

    notify_job(job)  # pass the stale instance

    data = layer.sent[0][1]["data"]
    assert data["status"] == "SUCCEEDED"  # re-fetched from the DB, not the stale arg
    assert data["progress"] == 100


def test_notify_job_noop_without_a_layer(monkeypatch):
    _use_layer(monkeypatch, None)
    notify_job(JobFactory())  # realtime unconfigured — must not raise


def test_notify_job_noop_when_job_deleted(monkeypatch):
    layer = _RecordingLayer()
    _use_layer(monkeypatch, layer)
    job = JobFactory()
    Job.objects.filter(pk=job.pk).delete()

    notify_job(job)  # the re-fetch finds nothing

    assert layer.sent == []


def test_notify_job_swallows_broadcast_errors(monkeypatch, caplog):
    _use_layer(monkeypatch, _BrokenLayer())
    job = JobFactory()

    with caplog.at_level(logging.WARNING, logger="jobs.realtime"):
        notify_job(job)  # best-effort: a layer outage must not raise

    assert any(r.getMessage() == "realtime.notify_failed" for r in caplog.records)
