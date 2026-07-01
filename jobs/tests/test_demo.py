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
    assert resp.cookies.get("csrftoken") is not None  # ensure_csrf_cookie fired on GET
