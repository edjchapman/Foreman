"""The demo page renders, wires the WebSocket client, and sets the CSRF cookie."""

import pytest

pytestmark = pytest.mark.django_db


def test_demo_page_renders(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert "jobs/demo.html" in [t.name for t in resp.templates]
    body = resp.content.decode()
    assert "/ws/jobs/" in body  # the page streams from the WebSocket endpoint
    assert 'id="run-sample"' in body
    assert 'id="run-bad"' in body
    # The reliability scenarios + operator action that make the machinery visible.
    assert 'id="run-flaky"' in body
    assert 'id="run-dlq"' in body
    assert 'id="redrive"' in body  # revealed by the jobDemo component on DEAD_LETTER
    assert 'id="metrics"' in body  # live queue-metrics strip (polls /metrics/summary)
    assert 'id="report"' in body  # download link, revealed by the jobDemo component on SUCCEEDED
    # Live queue board: wired via a vendored Alpine component (the ws/queue/ URL itself lives
    # in queue-board.js, so it's asserted by the consumer + e2e suites, not here).
    assert 'id="queue-board"' in body
    assert "x-data" in body
    assert "queue-board.js" in body  # substring, not the hashed name — storage-agnostic
    assert "alpine" in body
    assert resp.cookies.get("csrftoken") is not None  # ensure_csrf_cookie fired on GET


def test_demo_page_exposes_the_heal_window(client):
    """The dead-letter scenario's heal window is injected from settings, so the page
    and the fault source agree on one number."""
    from django.conf import settings

    resp = client.get("/")
    assert f'data-heal-after="{settings.DEMO_HEAL_AFTER_SECONDS}"' in resp.content.decode()
