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

# `or` (not a get() default): CI sets the env var from an optional repo variable,
# so it can be present-but-empty — an empty URL must still fall back to the demo.
BASE_URL = (os.environ.get("FOREMAN_E2E_URL") or "https://foreman-demo.up.railway.app").rstrip("/")
# Worker latency + a cold platform: generous, but a hung pipeline still fails.
STATUS_TIMEOUT_MS = 90_000

WS_JOB_PATH = re.compile(r"/ws/jobs/[0-9a-f-]{36}/$")
WS_QUEUE_PATH = re.compile(r"/ws/queue/$")


@pytest.fixture
def demo_page(page: Page) -> Page:
    page.goto(f"{BASE_URL}/")
    return page


def test_demo_page_loads(demo_page: Page) -> None:
    expect(demo_page).to_have_title("Foreman — live event-driven pipeline")
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

    # Assert on the status *badge*, not page text: the scenario copy now also mentions
    # SUCCEEDED/FAILED/DEAD_LETTER, so a bare get_by_text would be ambiguous.
    expect(demo_page.locator("#status")).to_have_text("SUCCEEDED", timeout=STATUS_TIMEOUT_MS)
    expect(demo_page.locator("#attempts")).to_have_text("1")
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

    expect(demo_page.locator("#status")).to_have_text("FAILED", timeout=STATUS_TIMEOUT_MS)
    # Non-retryable poison: exactly one attempt, no dead-letter churn.
    expect(demo_page.locator("#attempts")).to_have_text("1")


def test_flaky_job_retries_then_recovers_over_websocket(demo_page: Page) -> None:
    """A transient fault retries with backoff and recovers on its own — the attempt
    counter climbs past 1 and the job still lands SUCCEEDED, all over the socket."""
    frames: list[str] = []
    demo_page.on(
        "websocket",
        lambda ws: ws.on("framereceived", lambda payload: frames.append(str(payload))),
    )

    demo_page.get_by_role("button", name="Inject a flaky job").click()

    expect(demo_page.locator("#status")).to_have_text("SUCCEEDED", timeout=STATUS_TIMEOUT_MS)
    expect(demo_page.get_by_text('"rows_imported": 5')).to_be_visible()
    # Recovery, not first-try success: at least one earlier attempt was retried.
    retried = any(re.search(r'"attempts":\s*[2-9]', frame) for frame in frames)
    assert retried, f"expected a retry (attempts >= 2) before success, frames: {frames}"


def test_queue_board_populates_over_queue_websocket(page: Page) -> None:
    """The live board subscribes to the queue firehose on load (before any submit) and a
    submitted job lands as a SUCCEEDED card in the Done lane — pushed, never polled."""
    sockets: list[WebSocket] = []
    frames: list[str] = []
    job_polls: list[str] = []

    def on_websocket(ws: WebSocket) -> None:
        sockets.append(ws)
        ws.on("framereceived", lambda payload: frames.append(str(payload)))

    def on_request(request: Request) -> None:
        is_job_get = request.method == "GET" and "/api/v1/jobs/" in request.url
        if is_job_get and "/report/" not in request.url:
            job_polls.append(request.url)

    # Attach before navigation: the queue socket opens on page load, not on a click.
    page.on("websocket", on_websocket)
    page.on("request", on_request)
    page.goto(f"{BASE_URL}/")

    page.get_by_role("button", name="Import sample CSV").click()

    done = page.locator('#queue-board .lane[data-lane="done"]')
    expect(done.locator(".badge.SUCCEEDED").first).to_be_visible(timeout=STATUS_TIMEOUT_MS)

    assert any(WS_QUEUE_PATH.search(ws.url) for ws in sockets), (
        f"no queue WebSocket opened: {[w.url for w in sockets]}"
    )
    assert any("queue.snapshot" in f or "queue.job" in f for f in frames), "no queue frame received"
    assert job_polls == [], f"board polled the job endpoint: {job_polls}"


def test_dead_letter_then_redrive_recovers(demo_page: Page) -> None:
    """A job exhausts its retries into DEAD_LETTER; an operator redrive (once the
    simulated outage has healed) drives the same job back to SUCCEEDED."""
    demo_page.get_by_role("button", name="Send a job to the dead-letter queue").click()

    expect(demo_page.locator("#status")).to_have_text("DEAD_LETTER", timeout=STATUS_TIMEOUT_MS)
    redrive = demo_page.get_by_role("button", name="Redrive from dead-letter")
    expect(redrive).to_be_visible()

    # The fault heals `heal-after` seconds after submission; wait out the window (plus a
    # margin) so the redriven run imports cleanly instead of dead-lettering again.
    heal_after = int(demo_page.locator("body").get_attribute("data-heal-after") or "20")
    demo_page.wait_for_timeout((heal_after + 5) * 1000)

    redrive.click()
    expect(demo_page.locator("#status")).to_have_text("SUCCEEDED", timeout=STATUS_TIMEOUT_MS)
    expect(demo_page.get_by_text('"rows_imported": 5')).to_be_visible()
