/* Download tab: paste box, run deck, library list and track queue. */

const DEFAULT_HINT = $("#quick-hint").innerHTML;
const STATUS_KEYS = ["pending", "downloading", "done", "failed", "skipped"];

/* ------------------------------------------------------------- loading */
async function loadPlaylists() {
  const { playlists } = await api("/api/playlists");
  state.playlists = playlists;
  renderPlaylists();
  renderComparePlaylists();
}

async function loadItems() {
  if (!state.currentPlaylist) { state.items = []; renderQueue(); return; }
  const { items } = await api(`/api/playlists/${state.currentPlaylist}/items`);
  state.items = items;
  renderQueue();
}

async function selectPlaylist(id) {
  state.currentPlaylist = id;
  renderPlaylists();
  renderQueueHeader();
  await loadItems();
}

/* ---------------------------------------------------------------- deck */
function updateStats(stats) {
  state.stats = { ...stats };
  renderDeck();
}

function bumpStat(key, delta) {
  const stats = state.stats || {};
  state.stats = { ...stats, [key]: Math.max(0, (stats[key] || 0) + delta) };
  renderDeck();
}

function renderDeck() {
  const s = state.stats || {};
  const active = state.live.size || s.downloading || 0;
  const values = {
    total: s.total || 0, done: s.done || 0, skipped: s.skipped || 0,
    active, pending: s.pending || 0, failed: s.failed || 0,
  };
  Object.entries(values).forEach(([k, v]) => { $(`#stat-${k}`).textContent = v; });

  const total = values.total || 0;
  const pct = total ? Math.floor((values.done + values.skipped) / total * 100) : 0;
  $("#deck-pct").textContent = pct;
  const width = n => (total ? `${(n / total) * 100}%` : "0");
  $("#stack-bar .seg.done").style.width = width(values.done);
  $("#stack-bar .seg.skipped").style.width = width(values.skipped);
  $("#stack-bar .seg.active").style.width = width(active);
  $("#stack-bar .seg.failed").style.width = width(values.failed);

  $("#deck-caption").textContent = deckCaption(values);
  $("#run-deck").classList.toggle("running", !!state.run.running);
}

function deckCaption({ total, done, skipped, pending, failed, active }) {
  if (!total) return "Nothing queued yet";
  if (state.run.running && state.run.paused) return `Paused · ${pending} waiting`;
  if (state.run.running) return `Downloading ${active} · ${pending} waiting`;
  if (!pending && !failed) return `All ${total} tracks finished`;
  const parts = [`${done + skipped} of ${total} finished`];
  if (pending) parts.push(`${pending} ready to download`);
  if (failed) parts.push(`${failed} failed`);
  return parts.join(" · ");
}

function setRunUI() {
  const { running, paused } = state.run;
  $("#btn-start").hidden = running;
  $("#btn-pause").hidden = !running || paused;
  $("#btn-resume").hidden = !running || !paused;
  $("#btn-stop").hidden = !running;
  renderDeck();
}

/* ------------------------------------------------------------ playlists */
function renderPlaylists() {
  const box = $("#playlist-list");
  $("#lib-count").textContent = state.playlists.length;
  if (!state.playlists.length) {
    box.innerHTML = emptyState("music", "Your library is empty",
      "Paste a link above and choose <b>Add to library</b> or <b>Download now</b>.");
    return;
  }
  box.innerHTML = state.playlists.map(p => {
    const finished = (p.done || 0) + (p.skipped || 0);
    const pct = p.total ? Math.round(finished / p.total * 100) : 0;
    const active = state.currentPlaylist === p.id ? " active" : "";
    return `
      <div class="playlist${active}" data-id="${p.id}" tabindex="0" role="button"
           aria-pressed="${!!active}" aria-label="${esc(p.name)}, ${finished} of ${p.total} finished">
        <span class="pl-ring" style="--p:${pct}">${icon(p.source === "spotify" || p.source === "youtube" ? "music" : "folder")}</span>
        <span class="pl-name" title="${esc(p.name)}">${esc(p.name)}</span>
        <button class="icon bad" data-del="${p.id}" title="Remove from library" aria-label="Remove ${esc(p.name)}">${icon("x")}</button>
        <span class="pl-meta"><span>${finished}/${p.total}</span><span>${esc(p.source)}</span>${
          p.failed ? `<span class="bad">${p.failed} failed</span>` : ""}</span>
      </div>`;
  }).join("");
}

$("#playlist-list").addEventListener("click", async ev => {
  const del = ev.target.closest("[data-del]");
  if (del) { ev.stopPropagation(); removePlaylist(Number(del.dataset.del)); return; }
  const row = ev.target.closest(".playlist");
  if (row) selectPlaylist(Number(row.dataset.id));
});
$("#playlist-list").addEventListener("keydown", ev => {
  const row = ev.target.closest(".playlist");
  if (row && ev.target === row && (ev.key === "Enter" || ev.key === " ")) {
    ev.preventDefault();
    selectPlaylist(Number(row.dataset.id));
  }
});

async function removePlaylist(id) {
  const p = state.playlists.find(x => x.id === id);
  const ok = await confirmDialog({
    title: `Remove “${p ? p.name : "playlist"}”?`,
    body: "<p>Its queue is removed from the app. Files you already downloaded stay on disk.</p>",
    ok: "Remove", danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/playlists/${id}`, { method: "DELETE" });
  } catch (e) { toast("Could not remove", e.message, "error"); return; }
  if (state.currentPlaylist === id) { state.currentPlaylist = null; state.items = []; renderQueueHeader(); renderQueue(); }
  await loadPlaylists();
}

/* ---------------------------------------------------------------- queue */
function renderQueueHeader() {
  const p = state.playlists.find(x => x.id === state.currentPlaylist);
  $("#queue-title").textContent = p ? p.name : "Queue";
  $("#queue-sub").textContent = p
    ? `${p.total} tracks · ${p.source}`
    : "Pick a playlist from your library";
}

function filteredItems() {
  const q = $("#queue-filter").value.trim().toLowerCase();
  const st = state.statusFilter;
  return state.items.filter(it => {
    if (st && it.status !== st) return false;
    if (!q) return true;
    return (`${it.title || ""} ${it.artist || ""}`).toLowerCase().includes(q);
  });
}

function renderChipCounts() {
  const counts = Object.fromEntries(STATUS_KEYS.map(k => [k, 0]));
  state.items.forEach(it => { if (it.status in counts) counts[it.status]++; });
  $$("#status-chips [data-count]").forEach(b => {
    const key = b.dataset.count;
    const n = key ? counts[key] : state.items.length;
    b.textContent = state.items.length && n ? n : "";
  });
}

function renderQueue() {
  const body = $("#queue-body");
  renderChipCounts();
  state.queue.clear();
  const items = filteredItems();
  if (!items.length) {
    const msg = !state.currentPlaylist
      ? emptyState("download", "No playlist selected", "Choose one from your library, or paste a link above to add one.")
      : emptyState("search", "No tracks match", "Try a different filter or status.");
    body.innerHTML = `<tr class="table-empty"><td colspan="5">${msg}</td></tr>`;
    return;
  }
  const frag = document.createDocumentFragment();
  items.forEach((it, i) => {
    const tr = document.createElement("tr");
    tr.dataset.id = it.id;
    tr.dataset.num = i + 1;
    tr.className = it.status;
    tr.innerHTML = rowHtml(it, i + 1);
    frag.appendChild(tr);
    state.queue.set(it.id, tr);
  });
  body.replaceChildren(frag);
}

/** Re-render one row from state.items + state.live after an event. */
function patchRow(id) {
  const it = state.items.find(x => x.id === id);
  const tr = state.queue.get(id);
  if (!it || !tr) return;
  tr.className = it.status;
  tr.innerHTML = rowHtml(it, tr.dataset.num);
}

function rowHtml(it, num) {
  const live = state.live.get(it.id);
  const label = live && live.stage ? live.stage : it.status;
  return `
    <td class="col-num">${num}</td>
    <td><div class="t-title" title="${esc(it.title || it.url)}">${esc(it.title || it.url)}</div>${
      it.artist ? `<div class="t-artist">${esc(it.artist)}</div>` : ""}</td>
    <td class="col-status"><span class="pill ${it.status}">${esc(label)}</span></td>
    <td class="col-progress">${progressCell(it, live)}</td>
    <td class="col-actions"><span class="row-actions">${rowActions(it)}</span></td>`;
}

function rowActions(it) {
  const out = [];
  if (it.status === "failed") out.push(`<button class="icon" data-retry="${it.id}" title="Retry" aria-label="Retry">${icon("retry")}</button>`);
  if (it.status === "pending" || it.status === "failed") out.push(`<button class="icon" data-skip="${it.id}" title="Skip this track" aria-label="Skip">${icon("skip")}</button>`);
  if (it.filepath) out.push(`<button class="icon" data-open="${esc(it.filepath)}" title="Show in folder" aria-label="Show in folder">${icon("folder")}</button>`);
  return out.join("");
}

function progressCell(it, live) {
  if (live && live.retry) {
    const r = live.retry;
    return `<span class="meta warn" title="${esc(r.error || "")}">retrying in ${r.delay}s</span>`;
  }
  if (it.status === "downloading" || live) {
    const p = live || it;
    const pct = p.progress || 0;
    const extra = p.speed ? ` · ${fmtBytes(p.speed)}/s${p.eta ? " · " + fmtTime(p.eta) : ""}` : "";
    return `<div class="progress">${bar(pct)}<span class="meta">${pct.toFixed(0)}%${extra}</span></div>`;
  }
  if (it.status === "done") return `<div class="progress">${bar(100, "done")}<span class="meta">done</span></div>`;
  if (it.status === "failed") return `<span class="meta bad" title="${esc(it.error || "")}">${esc(it.error || "failed")}</span>`;
  if (it.status === "skipped") return `<span class="meta" title="${esc(it.reason || "")}">${esc(it.reason || "skipped")}</span>`;
  return `<span class="meta">${it.duration ? fmtTime(it.duration) : ""}</span>`;
}

$("#queue-filter").addEventListener("input", renderQueue);
$("#status-chips").addEventListener("click", ev => {
  const chip = ev.target.closest(".chip");
  if (!chip) return;
  state.statusFilter = chip.dataset.status;
  $$("#status-chips .chip").forEach(c => c.classList.toggle("active", c === chip));
  renderQueue();
});

$("#queue-body").addEventListener("click", async ev => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  const { retry, skip, open } = btn.dataset;
  try {
    if (retry) {
      await api(`/api/items/${retry}/retry`, { method: "POST", body: {} });
      setItem(Number(retry), { status: "pending", error: null });
    } else if (skip) {
      await api(`/api/items/${skip}`, { method: "DELETE" });
      setItem(Number(skip), { status: "skipped", reason: "skipped by you" });
    } else if (open) {
      await api("/api/reveal", { method: "POST", body: { path: open } });
    }
  } catch (e) { toast("Could not do that", e.message, "error"); }
});

/** Replace one item with an updated copy, then refresh its row and counts. */
function setItem(id, changes) {
  state.items = state.items.map(it => (it.id === id ? { ...it, ...changes } : it));
  patchRow(id);
  renderChipCounts();
}

/* --------------------------------------------------------- paste + import */
function selectedMode() {
  return ($("input[name=mode]:checked") || {}).value || "audio";
}

function setHint(html, resetAfterMs = 0) {
  $("#quick-hint").innerHTML = html;
  if (resetAfterMs) setTimeout(() => { $("#quick-hint").innerHTML = DEFAULT_HINT; }, resetAfterMs);
}

async function importLink(autostart) {
  const input = $("#quick-input");
  const location = input.value.trim();
  if (!location) {
    input.focus();
    toast("Paste a link first", "A playlist, album or video link — or a file path.", "warn");
    return;
  }
  const buttons = [$("#btn-quick"), $("#btn-import")];
  buttons.forEach(b => { b.disabled = true; });
  setHint("Reading the link…");
  try {
    await api("/api/playlist/import", {
      method: "POST",
      body: { source: $("#import-source").value, location, mode: selectedMode(), autostart },
    });
    input.value = "";
  } catch (e) {
    buttons.forEach(b => { b.disabled = false; });
    setHint(DEFAULT_HINT);
    toast("Could not read that link", e.message, "error");
  }
}

function onImportProgress(d) {
  if (d.stage === "reading") { setHint(esc(d.message || "Reading…")); return; }
  if (d.stage === "started") {
    if (!d.started) toast("Could not start", d.message, "error");
    return;
  }
  [$("#btn-quick"), $("#btn-import")].forEach(b => { b.disabled = false; });
  if (d.stage === "done") {
    setHint(`<b>${esc(d.message)}</b>`, 6000);
    toast("Imported", d.message, "ok");
    if (d.warning) toast("Heads up", d.warning, "warn");
    loadPlaylists().then(() => { if (d.playlist_id) selectPlaylist(d.playlist_id); });
  } else if (d.stage === "error") {
    setHint(DEFAULT_HINT);
    toast("Import failed", d.message || "", "error");
  }
}

$("#drop-form").addEventListener("submit", e => { e.preventDefault(); importLink(true); });
$("#btn-quick").addEventListener("click", () => importLink(true));
$("#btn-import").addEventListener("click", () => importLink(false));

// Dropping a link (or text) from a browser onto the paste box fills it in.
const dropForm = $("#drop-form");
["dragenter", "dragover"].forEach(t => dropForm.addEventListener(t, e => {
  e.preventDefault();
  dropForm.classList.add("dragging");
}));
["dragleave", "drop"].forEach(t => dropForm.addEventListener(t, () => dropForm.classList.remove("dragging")));
dropForm.addEventListener("drop", e => {
  e.preventDefault();
  const text = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
  if (text) { $("#quick-input").value = text.split("\n")[0].trim(); $("#quick-input").focus(); }
});

// "/" jumps to the paste box from anywhere that is not already a text field.
document.addEventListener("keydown", e => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey) return;
  if (e.target.closest("input, textarea, select, [contenteditable]")) return;
  e.preventDefault();
  showTab("download");
  $("#quick-input").focus();
});

/* --------------------------------------------------------- run controls */
function destPreview() {
  const base = $("#dest-path").value.trim() || state.settings.download_dir || "";
  const p = state.playlists.find(x => x.id === state.currentPlaylist);
  const many = !p || (p.total || 0) > 1;
  if ($("#dest-subfolder").checked && many) {
    return `${base}/${p ? p.name : "‹playlist name›"}/`;
  }
  return `${base}/`;
}
const refreshDestPreview = () => { $("#dest-preview").textContent = destPreview(); };

function openDestModal() {
  const s = state.settings || {};
  $("#dest-path").value = s.download_dir || "";
  $("#dest-subfolder").checked = s.make_playlist_subfolder !== false;
  const p = state.playlists.find(x => x.id === state.currentPlaylist);
  $("#dest-title").textContent = p ? `Download “${p.name}”` : "Download everything pending";
  refreshDestPreview();
  $("#dest-go").disabled = false;
  openModal("dest-modal");
}

$("#dest-path").addEventListener("input", refreshDestPreview);
$("#dest-subfolder").addEventListener("change", refreshDestPreview);
$("#dest-cancel").addEventListener("click", () => closeModal("dest-modal"));
$("#dest-close").addEventListener("click", () => closeModal("dest-modal"));
$("#dest-choose").addEventListener("click", () => openBrowser($("#dest-path").value || "", path => {
  $("#dest-path").value = path;
  refreshDestPreview();
}));

$("#dest-go").addEventListener("click", async () => {
  const dir = $("#dest-path").value.trim();
  if (!dir) { toast("Pick a folder", "Choose where the files should go.", "warn"); return; }
  $("#dest-go").disabled = true;
  try {
    const saved = await api("/api/settings", {
      method: "POST",
      body: { download_dir: dir, make_playlist_subfolder: $("#dest-subfolder").checked },
    });
    state.settings = saved.settings;
    fillSettings(saved.settings);
    await api("/api/download/start", {
      method: "POST",
      body: { playlist_id: state.currentPlaylist, retry_failed: false },
    });
    closeModal("dest-modal");
  } catch (e) {
    toast("Could not start", e.message, "error");
  } finally {
    $("#dest-go").disabled = false;
  }
});

const runAction = path => async () => {
  try { await api(path, { method: "POST" }); }
  catch (e) { toast("Could not do that", e.message, "error"); }
};
$("#btn-start").addEventListener("click", openDestModal);
$("#btn-pause").addEventListener("click", runAction("/api/download/pause"));
$("#btn-resume").addEventListener("click", runAction("/api/download/resume"));
$("#btn-stop").addEventListener("click", runAction("/api/download/stop"));
$("#btn-retry").addEventListener("click", async () => {
  try {
    const r = await api("/api/items/retry-failed", {
      method: "POST", body: { playlist_id: state.currentPlaylist },
    });
    toast(r.requeued ? "Re-queued" : "Nothing to retry",
      r.requeued ? `${r.requeued} failed tracks are pending again. Press Start to download them.` : "",
      r.requeued ? "ok" : "");
    await refreshAll();
  } catch (e) { toast("Nothing to retry", e.message, "warn"); }
});

async function refreshAll() {
  const s = await api("/api/state");
  state.run = s.run;
  updateStats(s.stats);
  setRunUI();
  await loadPlaylists();
  if (state.currentPlaylist) await loadItems();
}
