/* Live updates from the backend (Server-Sent Events) and the activity log. */

const LOG_MAX = 500;

function connectEvents() {
  const es = new EventSource("/api/events");
  const conn = $("#conn");
  es.onopen = () => { conn.className = "conn live"; conn.textContent = "connected"; };
  es.onerror = () => { conn.className = "conn down"; conn.textContent = "reconnecting…"; };
  es.onmessage = ev => {
    let e;
    try { e = JSON.parse(ev.data); } catch { return; }
    const handler = EVENT_HANDLERS[e.type];
    if (handler) handler(e.data || {});
  };
}

function patchItem(id, changes) {
  if (state.items.some(x => x.id === id)) setItem(id, changes);
}

const EVENT_HANDLERS = {
  item_started(d) {
    state.live.set(d.id, { progress: 0, stage: "downloading" });
    patchItem(d.id, { status: "downloading", progress: 0 });
    bumpStat("downloading", 1);
  },
  item_progress(d) {
    state.live.set(d.id, { ...(state.live.get(d.id) || {}), ...d, retry: null });
    patchRow(d.id);
  },
  item_stage(d) {
    state.live.set(d.id, { ...(state.live.get(d.id) || {}), stage: d.stage });
    patchRow(d.id);
  },
  item_retrying(d) {
    state.live.set(d.id, { progress: 0, stage: `retry ${d.attempt}/${d.of}`, retry: d });
    patchRow(d.id);
  },
  item_done(d) {
    state.live.delete(d.id);
    const skipped = !!d.skipped;
    patchItem(d.id, {
      status: skipped ? "skipped" : "done", progress: 100,
      filepath: d.filepath, reason: d.reason || (skipped ? "already have it" : ""),
    });
    bumpStat("downloading", -1);
    bumpStat(skipped ? "skipped" : "done", 1);
    bumpStat("pending", -1);
  },
  item_failed(d) {
    state.live.delete(d.id);
    patchItem(d.id, { status: "failed", error: d.error });
    bumpStat("downloading", -1);
    bumpStat("failed", 1);
    bumpStat("pending", -1);
    logLine("error", `${d.title || "item"}: ${d.error}`);
  },
  run_started(d) {
    state.run = { ...state.run, running: true, paused: false };
    setRunUI();
    toast("Downloading", `${d.queued} tracks · ${d.workers} at a time`, "ok");
  },
  run_paused() { state.run = { ...state.run, paused: true }; setRunUI(); },
  run_resumed() { state.run = { ...state.run, paused: false }; setRunUI(); },
  run_stopping() { state.run = { ...state.run, running: false }; setRunUI(); },
  run_finished(d) {
    state.run = { ...state.run, running: false, paused: false };
    state.live.clear();
    setRunUI();
    toast("Finished", `${d.stats.done} done · ${d.stats.failed} failed`, d.stats.failed ? "warn" : "ok");
    refreshAll();
  },
  playlists_changed() { loadPlaylists(); },
  import_progress: onImportProgress,
  compare_progress: onCompareProgress,
  update_progress: onUpdateProgress,
  tools_progress: onToolsProgress,
  log(d) { logLine(d.level, d.message); },
};

/* -------------------------------------------------------------------- log */
function logLine(level, message) {
  const view = $("#log-view");
  const el = document.createElement("div");
  el.className = `log-line ${level || "info"}`;
  el.innerHTML = `<time>${new Date().toLocaleTimeString()}</time><span class="lvl">${esc(level || "info")}</span><span>${esc(message)}</span>`;
  const nearBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 120;
  view.appendChild(el);
  while (view.childElementCount > LOG_MAX) view.removeChild(view.firstChild);
  if (nearBottom) view.scrollTop = view.scrollHeight;
  if (level === "error" && !$("#tab-log").classList.contains("active")) $("#log-badge").hidden = false;
}

$("#btn-clear-log").addEventListener("click", () => $("#log-view").replaceChildren());
$("#log-errors-only").addEventListener("change", e =>
  $("#log-view").classList.toggle("errors-only", e.target.checked));
