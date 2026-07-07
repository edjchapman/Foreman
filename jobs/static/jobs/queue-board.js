/* Live queue board — Alpine component.
 *
 * Consumes the JSON-over-WebSocket firehose at `ws/queue/`: a one-shot `queue.snapshot`
 * of the newest jobs on connect, then a `queue.job` per transition. State is a single
 * `jobs` array (newest-first); three computed lanes filter it by status group so a card
 * moves between lanes when its status changes, with no manual DOM re-parenting.
 *
 * Registered on `alpine:init` (this file loads before alpine.min.js), so the component is
 * defined before Alpine boots. There is deliberately NO fetch of /api/v1/jobs/ — the board
 * is push-only, preserving the demo's no-polling-for-job-status invariant.
 */
document.addEventListener("alpine:init", () => {
  const BOARD_SIZE = 20;
  const FLASH_MS = 1000;
  const DONE = new Set(["SUCCEEDED", "FAILED", "DEAD_LETTER"]);

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
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${scheme}://${location.host}/ws/queue/`);
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "queue.snapshot") this.jobs = msg.jobs.slice(0, BOARD_SIZE);
        else if (msg.type === "queue.job") this.upsert(msg.job);
      };
    },

    // Replace-in-place by id, else prepend; cap at BOARD_SIZE (drop oldest). Upsert-by-id
    // absorbs snapshot/live overlap and any duplicate frames.
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
});
