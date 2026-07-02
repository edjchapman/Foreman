"""Browser checks against the live demo — the executable form of the manual
smoke checks in docs/deploy.md (page load, sample import with live WebSocket
status, report download, poison-job failure path).

Run via `make e2e`; point FOREMAN_E2E_URL at another deployment to retarget.
Deliberately outside `make ci`: needs a Chromium install and a live platform.
"""

import os
import re

import pytest
from playwright.sync_api import Page, Request, WebSocket, expect

BASE_URL = os.environ.get("FOREMAN_E2E_URL", "https://foreman-demo.up.railway.app").rstrip("/")
# Worker latency + a cold platform: generous, but a hung pipeline still fails.
STATUS_TIMEOUT_MS = 90_000

WS_JOB_PATH = re.compile(r"/ws/jobs/[0-9a-f-]{36}/$")


@pytest.fixture
def demo_page(page: Page) -> Page:
    page.goto(f"{BASE_URL}/")
    return page


def test_demo_page_loads(demo_page: Page) -> None:
    expect(demo_page).to_have_title("Foreman — live job status")
    expect(demo_page.get_by_role("button", name="Import sample CSV")).to_be_visible()
    expect(demo_page.get_by_role("button", name="Try an unsupported source")).to_be_visible()


def test_sample_import_succeeds_live_over_websocket(demo_page: Page) -> None:
    sockets: list[WebSocket] = []
    frames: list[str] = []
    job_polls: list[str] = []

    def on_websocket(ws: WebSocket) -> None:
        sockets.append(ws)
        ws.on("framereceived", lambda payload: frames.append(str(payload)))

    def on_request(request: Request) -> None:
        # Any GET of the job resource would mean the page fell back to polling.
        is_job_get = request.method == "GET" and "/api/v1/jobs/" in request.url
        if is_job_get and "/report/" not in request.url:
            job_polls.append(request.url)

    demo_page.on("websocket", on_websocket)
    demo_page.on("request", on_request)

    demo_page.get_by_role("button", name="Import sample CSV").click()

    expect(demo_page.get_by_text("SUCCEEDED")).to_be_visible(timeout=STATUS_TIMEOUT_MS)
    expect(demo_page.get_by_text("attempt 1")).to_be_visible()
    expect(demo_page.get_by_text('"rows_imported": 5')).to_be_visible()

    # The status updates must have arrived over the job's WebSocket, not polling.
    assert any(WS_JOB_PATH.search(ws.url) for ws in sockets), f"no job WebSocket opened: {sockets}"
    assert any("SUCCEEDED" in frame for frame in frames), "no SUCCEEDED frame received"
    assert job_polls == [], f"page polled the job endpoint: {job_polls}"

    # The report link streams the imported records as CSV.
    href = demo_page.get_by_role("link", name="Download report (CSV)").get_attribute("href")
    assert href is not None
    report = demo_page.request.get(f"{BASE_URL}{href}")
    assert report.ok
    assert report.text().startswith("external_id,")


def test_unsupported_source_fails_without_retries(demo_page: Page) -> None:
    demo_page.get_by_role("button", name="Try an unsupported source").click()

    expect(demo_page.get_by_text("FAILED")).to_be_visible(timeout=STATUS_TIMEOUT_MS)
    # Non-retryable poison: exactly one attempt, no dead-letter churn.
    expect(demo_page.get_by_text("attempt 1")).to_be_visible()
