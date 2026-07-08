/* Alpine components for the demo page — registered on `alpine:init`, before Alpine boots.
 *
 * Three components, one per live region, each owning its own data source (they share no state):
 *   - queueBoard   — the whole-queue firehose over `/ws/queue/` (a snapshot on connect, then a
 *                    frame per transition); three computed lanes filter by status group.
 *   - jobDemo      — the scenario buttons + the per-job panel: POST a job, then stream ONLY its
 *                    status over `/ws/jobs/<id>/`. Push-only — it never polls `/api/v1/jobs/<id>`
 *                    (the E2E suite asserts this). Owns submit / redrive / pipeline-stage derivation.
 *   - metricsStrip — the queue-metrics tiles, polled from `/api/v1/metrics/summary`.
 *
 * Consolidating the page on Alpine keeps one reactive model — no imperative `getElementById` — with
 * NO build step (Alpine is vendored). Loaded (deferred) before alpine.min.js so this `alpine:init`
 * listener is registered before Alpine dispatches the event. The file keeps its name for the test
 * contract even though it now holds every demo component. See ADR 0004.
 */
document.addEventListener("alpine:init", () => {
  const BOARD_SIZE = 20;
  const FLASH_MS = 1000;
  const METRICS_POLL_MS = 1500;
  const DONE = new Set(["SUCCEEDED", "FAILED", "DEAD_LETTER"]);

  const csrf = () =>
    document.cookie.split("; ").find((r) => r.startsWith("csrftoken="))?.split("=")[1];
  const wsUrl = (path) =>
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;

  // Live queue board: newest-first `jobs`, filtered into three lanes so a card moves between lanes
  // as its status changes — no manual DOM re-parenting. Push-only (no fetch of /api/v1/jobs/).
  Alpine.data("queueBoard", () => ({
    jobs: [],
    flashId: null,

    get queued() {
      return this.jobs.filter((j) => j.status === "PENDING");
    },
    get processing() {
      return this.jobs.filter((j) => j.status === "PROCESSING");
    },
    get done() {
      return this.jobs.filter((j) => DONE.has(j.status));
    },

    init() {
      const ws = new WebSocket(wsUrl("/ws/queue/"));
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "queue.snapshot") this.jobs = msg.jobs.slice(0, BOARD_SIZE);
        else if (msg.type === "queue.job") this.upsert(msg.job);
      };
    },

    // Replace-in-place by id, else prepend; cap at BOARD_SIZE (drop oldest). Upsert-by-id absorbs
    // snapshot/live overlap and any duplicate frames.
    upsert(job) {
      const i = this.jobs.findIndex((j) => j.id === job.id);
      if (i === -1) this.jobs.unshift(job);
      else this.jobs.splice(i, 1, job);
      if (this.jobs.length > BOARD_SIZE) this.jobs.length = BOARD_SIZE;
      this.flashId = job.id;
      setTimeout(() => {
        if (this.flashId === job.id) this.flashId = null;
      }, FLASH_MS);
    },
  }));

  // Scenario buttons + the per-job panel they drive. `job` is the latest snapshot (null until a
  // scenario runs); the panel binds to it reactively. Status arrives ONLY over the WebSocket.
  Alpine.data("jobDemo", () => ({
    job: null,
    submitting: false,
    ws: null,
    healAfter: document.body.dataset.healAfter || "20",

    // Each button maps to a source the ingest layer resolves; `dlq` uses the settings-driven
    // heal window so the page and the fault source agree on one number.
    sources: { sample: "sample:properties.csv", flaky: "fault:flaky", bad: "s3://bucket/data.csv" },
    run(kind) {
      const source = kind === "dlq" ? `fault:heal-after:${this.healAfter}` : this.sources[kind];
      this.submit(source);
    },

    async submit(source) {
      if (this.ws) {
        this.ws.close();
        this.ws = null;
      }
      this.job = null;
      this.submitting = true;
      const res = await fetch("/api/v1/jobs/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ job_type: "property_csv_import", payload: { source } }),
      });
      this.job = await res.json();
      this.submitting = false;
      this.stream(this.job.id);
    },

    stream(id) {
      // The browser's Origin header matches this page → AllowedHostsOriginValidator passes.
      this.ws = new WebSocket(wsUrl(`/ws/jobs/${id}/`));
      this.ws.onmessage = (e) => {
        this.job = JSON.parse(e.data);
      };
    },

    async redrive() {
      if (!this.job) return;
      await fetch(`/api/v1/jobs/${this.job.id}/redrive/`, {
        method: "POST",
        headers: { "X-CSRFToken": csrf() },
      });
      // Reopen the socket for a fresh snapshot (PENDING) and stream the recovery.
      if (this.ws) this.ws.close();
      this.stream(this.job.id);
    },

    // --- derived view state (was the imperative render()) ---
    get open() {
      return this.submitting || this.job !== null;
    },
    get statusText() {
      return this.job ? this.job.status : "…";
    },
    get isSucceeded() {
      return !!this.job && this.job.status === "SUCCEEDED";
    },
    get isDeadLetter() {
      return !!this.job && this.job.status === "DEAD_LETTER";
    },
    get reportHref() {
      return this.job ? `/api/v1/jobs/${this.job.id}/report/` : "#";
    },
    get detail() {
      if (this.submitting) return "submitting…";
      if (!this.job) return "";
      const d = this.job.error ? { error: this.job.error } : this.job.result;
      return d ? JSON.stringify(d, null, 2) : "";
    },
    // The "done"/"active"/"fail" modifier for one pipeline stage, from the current status.
    stageMod(stage) {
      const s = this.job && this.job.status;
      const map = { submitted: "done", queued: "", worker: "", result: "" };
      if (s === "PENDING") map.queued = "active";
      else if (s === "PROCESSING") {
        map.queued = "done";
        map.worker = "active";
      } else if (s) {
        map.queued = "done";
        map.worker = "done";
        map.result = s === "SUCCEEDED" ? "done" : "fail";
      }
      return map[stage] || "";
    },
  }));

  // Queue-metrics tiles, polled from the JSON summary (independent of any one job).
  Alpine.data("metricsStrip", () => ({
    m: { PENDING: "–", PROCESSING: "–", SUCCEEDED: "–", FAILED: "–", DEAD_LETTER: "–", outbox: "–", retry: "–" },

    init() {
      this.poll();
      setInterval(() => this.poll(), METRICS_POLL_MS);
    },

    async poll() {
      try {
        const res = await fetch("/api/v1/metrics/summary");
        const d = await res.json();
        const s = d.jobs_by_status || {};
        this.m = {
          PENDING: s.PENDING ?? 0,
          PROCESSING: s.PROCESSING ?? 0,
          SUCCEEDED: s.SUCCEEDED ?? 0,
          FAILED: s.FAILED ?? 0,
          DEAD_LETTER: s.DEAD_LETTER ?? 0,
          outbox: d.outbox_pending ?? "–",
          retry: d.retry_scheduled ?? "–",
        };
      } catch (_) {
        /* transient network blip — the next tick retries */
      }
    },
  }));
});
