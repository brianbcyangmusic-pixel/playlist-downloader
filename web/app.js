/* Playlist Downloader — front-end logic (vanilla JS, no build step). */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  settings: {},
  defaults: {},
  tools: {},
  playlists: [],
  currentPlaylist: null,
  items: [],
  run: { running: false, paused: false, active: [] },
  queue: new Map(),          // item id -> row element
  live: new Map(),           // item id -> live progress payload
  lastCompare: null,
  browse: null,
  update: {},
};

/* ------------------------------------------------------------------ utils */
function toast(title, body = "", kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<b>${esc(title)}</b>${body ? `<span>${esc(body)}</span>` : ""}`;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s, transform .3s";
    el.style.opacity = "0";
    el.style.transform = "translateX(24px)";
    setTimeout(() => el.remove(), 320);
  }, kind === "error" ? 7000 : 3800);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtTime(s) {
  if (s === null || s === undefined) return "";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
           : `${m}:${String(sec).padStart(2, "0")}`;
}
function bar(pct, cls = "") {
  return `<div class="track"><i class="${cls}" style="width:${Math.min(100, pct || 0)}%"></i></div>`;
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) {
    // The session expired or was revoked: auth.js puts the gate back up.
    window.dispatchEvent(new CustomEvent("auth-required"));
  }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `${res.status} ${res.statusText}`);
  }
  return data;
}

/* ------------------------------------------------------------------- tabs */
$$(".tab").forEach(tab => tab.addEventListener("click", () => {
  $$(".tab").forEach(t => t.classList.remove("active"));
  $$(".panel").forEach(p => p.classList.remove("active"));
  tab.classList.add("active");
  $(`#tab-${tab.dataset.tab}`).classList.add("active");
}));
$$(".mini-tab").forEach(tab => tab.addEventListener("click", () => {
  $$(".mini-tab").forEach(t => t.classList.remove("active"));
  $$(".cres").forEach(c => c.classList.remove("active"));
  tab.classList.add("active");
  $(`#cres-${tab.dataset.cres}`).classList.add("active");
}));

/* -------------------------------------------------------------- bootstrap */
async function loadState() {
  const s = await api("/api/state");
  state.settings = s.settings;
  state.defaults = s.defaults;
  state.tools = s.tools;
  state.run = s.run;
  $("#version").textContent = `v${s.version} · ${s.platform}`;
  $("#update-current").textContent = `v${s.version}`;
  fillSettings(s.settings);
  renderTools(s.tools);
  renderToolsSetup(s.tools);
  updateStats(s.stats);
  await loadPlaylists();
}

async function loadPlaylists() {
  const { playlists } = await api("/api/playlists");
  state.playlists = playlists;
  renderPlaylists();
  renderComparePlaylists();
}

/* --------------------------------------------------------------- settings */
const BOOL_KEYS = [
  "tag_metadata", "embed_cover_art", "write_cover_file", "rename_to_metadata",
  "use_archive", "skip_existing_files", "resolve_missing_to_youtube",
  "auto_retry_failed", "dry_run", "make_playlist_subfolder", "auto_check_updates",
];
const TEXT_KEYS = [
  "download_dir", "audio_format", "audio_quality", "audio_filename",
  "video_format", "video_quality", "video_filename", "rate_limit_kbps",
  "retries", "retry_delay_seconds", "socket_timeout", "sleep_between_requests",
  "proxy", "cookies_from_browser", "ffmpeg_path", "spotify_client_id",
  "spotify_client_secret",
];
const RANGE_KEYS = ["concurrent_downloads", "concurrent_fragments", "item_retries"];

function fillSettings(s) {
  TEXT_KEYS.forEach(k => {
    const el = $(`#set-${k}`);
    if (el) el.value = s[k] ?? "";
  });
  BOOL_KEYS.forEach(k => {
    const el = $(`#set-${k}`);
    if (el) el.checked = !!s[k];
  });
  RANGE_KEYS.forEach(k => {
    const el = $(`#set-${k}`);
    if (el) el.value = s[k];
  });
  $("#lbl-workers").textContent = s.concurrent_downloads;
  $("#lbl-frags").textContent = s.concurrent_fragments;
  $("#lbl-item-retries").textContent = s.item_retries;
}
$("#set-concurrent_downloads").addEventListener("input", e =>
  $("#lbl-workers").textContent = e.target.value);
$("#set-concurrent_fragments").addEventListener("input", e =>
  $("#lbl-frags").textContent = e.target.value);
$("#set-item_retries").addEventListener("input", e =>
  $("#lbl-item-retries").textContent = e.target.value);

async function saveSettings() {
  const body = {};
  TEXT_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) body[k] = el.value; });
  BOOL_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) body[k] = el.checked; });
  RANGE_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) body[k] = Number(el.value); });
  try {
    const res = await api("/api/settings", { method: "POST", body });
    state.settings = res.settings;
    renderTools(res.tools);
    toast("Settings saved", "", "ok");
  } catch (e) { toast("Could not save", e.message, "error"); }
}
$("#btn-save").addEventListener("click", saveSettings);
$("#btn-reveal").addEventListener("click", async () => {
  try { await api("/api/reveal", { method: "POST", body: { path: $("#set-download_dir").value } }); }
  catch (e) { toast("Could not open folder", e.message, "error"); }
});

function renderTools(t) {
  const js = (t.js_runtimes && t.js_runtimes.length) ? t.js_runtimes.join("/") : "";
  $("#tool-status").innerHTML = `
    <span class="tool"><i class="dot ${t.ytdlp_ok ? "ok" : ""}"></i>yt-dlp <b>${t.ytdlp_ok ? "ready" : "missing"}</b></span>
    <span class="tool"><i class="dot ${t.ffmpeg_ok ? "ok" : ""}"></i>ffmpeg <b>${t.ffmpeg_ok ? "ready" : "missing"}</b></span>
    <span class="tool"><i class="dot ${t.ejs_ok ? "ok" : ""}"></i>JS challenge <b>${t.ejs_ok ? (js || "ready") : "missing"}</b></span>
    <span class="tool"><i class="dot ${t.rapidfuzz_installed ? "ok" : ""}"></i>fast match <b>${t.rapidfuzz_installed ? "ready" : "slow"}</b></span>
  `;
}

/* ------------------------------------------------------------------ stats */
function updateStats(stats) {
  $("#stat-total").textContent = stats.total;
  $("#stat-done").textContent = stats.done;
  $("#stat-skipped").textContent = stats.skipped || 0;
  $("#stat-pending").textContent = stats.pending;
  $("#stat-failed").textContent = stats.failed;
  $("#stat-active").textContent = state.live.size || stats.downloading || 0;
}

/* -------------------------------------------------------------- playlists */
function renderPlaylists() {
  const box = $("#playlist-list");
  if (!state.playlists.length) {
    box.innerHTML = `<p class="empty">No playlists yet.</p>`;
    return;
  }
  box.innerHTML = state.playlists.map(p => {
    const done = p.done || 0, skipped = p.skipped || 0, total = p.total || 1;
    const pct = Math.round((done + skipped) / total * 100);
    const active = state.currentPlaylist === p.id ? "active" : "";
    return `
      <div class="playlist ${active}" data-id="${p.id}">
        <div class="playlist-top">
          <span class="playlist-name" title="${esc(p.name)}">${esc(p.name)}</span>
          <button class="icon-btn" data-del="${p.id}" title="Remove playlist">✕</button>
        </div>
        <div class="playlist-meta">
          <span>${esc(p.source)} · ${done + skipped}/${p.total} done</span>
          ${p.failed ? `<span style="color:var(--bad)">${p.failed} failed</span>` : ""}
        </div>
        <div class="bar"><i style="width:${pct}%"></i></div>
      </div>`;
  }).join("");

  $$(".playlist", box).forEach(el => el.addEventListener("click", ev => {
    if (ev.target.dataset.del) return;
    selectPlaylist(Number(el.dataset.id));
  }));
  $$("[data-del]", box).forEach(btn => btn.addEventListener("click", async ev => {
    ev.stopPropagation();
    const id = Number(btn.dataset.del);
    const p = state.playlists.find(x => x.id === id);
    if (!confirm(`Remove “${p.name}” and its queue? Downloaded files are not deleted.`)) return;
    await api(`/api/playlists/${id}`, { method: "DELETE" });
    if (state.currentPlaylist === id) { state.currentPlaylist = null; state.items = []; renderQueue(); }
    await loadPlaylists();
  }));
}

async function selectPlaylist(id) {
  state.currentPlaylist = id;
  const p = state.playlists.find(x => x.id === id);
  $("#queue-title").textContent = p ? `Queue — ${p.name}` : "Queue";
  renderPlaylists();
  await loadItems();
}

async function loadItems() {
  if (!state.currentPlaylist) { state.items = []; renderQueue(); return; }
  const { items } = await api(`/api/playlists/${state.currentPlaylist}/items`);
  state.items = items;
  renderQueue();
}

/* ------------------------------------------------------------------ queue */
function filteredItems() {
  const q = $("#queue-filter").value.trim().toLowerCase();
  const st = $("#queue-status-filter").value;
  return state.items.filter(it => {
    if (st && it.status !== st) return false;
    if (!q) return true;
    return (`${it.title || ""} ${it.artist || ""}`).toLowerCase().includes(q);
  });
}

function renderQueue() {
  const body = $("#queue-body");
  const items = filteredItems();
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">${state.currentPlaylist ? "No items match this filter." : "Import a playlist to get started."}</td></tr>`;
    state.queue.clear();
    return;
  }
  const frag = document.createDocumentFragment();
  state.queue.clear();

  items.forEach((it, i) => {
    const tr = document.createElement("tr");
    tr.dataset.id = it.id;
    tr.className = it.status;
    tr.innerHTML = rowHtml(it, i + 1);
    frag.appendChild(tr);
    state.queue.set(it.id, tr);
  });
  body.replaceChildren(frag);
  // Re-apply live progress so a re-render does not lose the moving bars.
  state.live.forEach((p, id) => applyProgress(id, p));
}

function rowHtml(it, num) {
  return `
    <td class="col-num">${num}</td>
    <td><div class="cell-title" title="${esc(it.title || it.url)}">${esc(it.title || it.url)}</div></td>
    <td class="col-artist"><div class="cell-title" title="${esc(it.artist || "")}">${esc(it.artist || "")}</div></td>
    <td class="col-status"><span class="pill ${it.status}">${it.status}</span></td>
    <td class="col-progress">${progressCell(it)}</td>
    <td class="col-actions">${it.status === "failed"
      ? `<button class="icon-btn" data-retry="${it.id}" title="Retry">↻</button>` : ""}
      ${it.filepath ? `<button class="icon-btn" data-open="${esc(it.filepath)}" title="Show in folder">📁</button>` : ""}
    </td>`;
}

function progressCell(it) {
  if (it.status === "downloading") {
    return `<div class="progress">${bar(it.progress)}<span class="meta">${(it.progress || 0).toFixed(0)}%</span></div>`;
  }
  if (it.status === "done") {
    return `<div class="progress">${bar(100, "done")}<span class="meta">done</span></div>`;
  }
  if (it.status === "failed") {
    return `<span class="meta" title="${esc(it.error || "")}" style="color:var(--bad)">${esc((it.error || "failed").slice(0, 46))}</span>`;
  }
  return `<span class="meta">${it.duration ? fmtTime(it.duration) : ""}</span>`;
}

function applyProgress(id, p) {
  const tr = state.queue.get(id);
  if (!tr) return;
  const cell = $(".col-progress", tr);
  if (cell) {
    cell.innerHTML = `<div class="progress">${bar(p.progress)}<span class="meta">${
      (p.progress || 0).toFixed(0)}% · ${fmtBytes(p.speed)}/s${p.eta ? " · " + fmtTime(p.eta) : ""}</span></div>`;
  }
  const pill = $(".pill", tr);
  if (pill) { pill.className = "pill downloading"; pill.textContent = p.stage || "downloading"; }
}
$("#queue-filter").addEventListener("input", () => renderQueue());
$("#queue-status-filter").addEventListener("change", () => renderQueue());

$("#queue-body").addEventListener("click", async ev => {
  const retry = ev.target.dataset.retry;
  const open = ev.target.dataset.open;
  if (retry) {
    await api(`/api/items/${retry}/retry`, { method: "POST", body: {} });
    const item = state.items.find(x => x.id === Number(retry));
    if (item) { item.status = "pending"; item.error = null; }
    renderQueue();
  } else if (open) {
    await api("/api/reveal", { method: "POST", body: { path: open } });
  }
});

/* ------------------------------------------------------------- download UI */
$("#btn-import").addEventListener("click", doImport);
async function doImport() {
  const location = $("#import-input").value.trim();
  const source = $("#import-source").value;
  const mode = $("#import-mode").value;
  if (!location) { toast("Nothing to import", "Paste a link or file path.", "warn"); return; }
  $("#btn-import").disabled = true;
  $("#import-hint").textContent = "Reading playlist…";
  try {
    await api("/api/playlist/import", { method: "POST", body: { source, location, mode } });
  } catch (e) {
    $("#import-hint").textContent = "";
    $("#btn-import").disabled = false;
    toast("Import failed", e.message, "error");
  }
}

/* ------------------------------------------------- download destination UI */
function destPreview() {
  const base = $("#dest-path").value.trim() || state.settings?.download_dir || "";
  const sub = $("#dest-subfolder").checked;
  const p = state.playlists.find(x => x.id === state.currentPlaylist);
  const many = !state.currentPlaylist || (p ? (p.total || 0) > 1 : false);
  if (sub && many) {
    const name = (state.currentPlaylist && p) ? p.name : "Playlist name";
    return `Files go to: ${base}/${name}/`;
  }
  return `Files go to: ${base}/`;
}

function openDestModal() {
  const s = state.settings || {};
  $("#dest-path").value = s.download_dir || "";
  $("#dest-subfolder").checked = s.make_playlist_subfolder !== false;
  $("#dest-preview").textContent = destPreview();
  $("#dest-modal").hidden = false;
  $("#dest-go").disabled = false;
  $("#dest-path").focus();
}
function closeDestModal() { $("#dest-modal").hidden = true; }

$("#dest-path").addEventListener("input", () => { $("#dest-preview").textContent = destPreview(); });
$("#dest-subfolder").addEventListener("change", () => { $("#dest-preview").textContent = destPreview(); });
$("#dest-cancel").addEventListener("click", closeDestModal);
$("#dest-close").addEventListener("click", closeDestModal);
$("#dest-modal").addEventListener("click", e => { if (e.target.id === "dest-modal") closeDestModal(); });
$("#dest-choose").addEventListener("click", async () => {
  await openBrowser($("#dest-path").value || "", path => {
    $("#dest-path").value = path;
    $("#dest-preview").textContent = destPreview();
    closeModal();
  });
});

$("#btn-start").addEventListener("click", openDestModal);
$("#dest-go").addEventListener("click", async () => {
  const dir = $("#dest-path").value.trim();
  if (!dir) { toast("Pick a folder", "Choose where the files should go.", "warn"); return; }
  const subfolder = $("#dest-subfolder").checked;
  $("#dest-go").disabled = true;
  try {
    const saved = await api("/api/settings", {
      method: "POST", body: { download_dir: dir, make_playlist_subfolder: subfolder },
    });
    state.settings = saved.settings;
    const res = await api("/api/download/start", {
      method: "POST",
      body: { playlist_id: state.currentPlaylist, retry_failed: false },
    });
    closeDestModal();
    toast("Download started", `${res.queued} items queued across ${res.workers} workers.`, "ok");
  } catch (e) {
    toast("Could not start", e.message, "error");
  } finally {
    $("#dest-go").disabled = false;
  }
});
$("#btn-pause").addEventListener("click", () => api("/api/download/pause", { method: "POST" }));

/* --------------------------------------------------------- quick download */
$("#btn-quick").addEventListener("click", quickDownload);
$("#quick-input").addEventListener("keydown", e => { if (e.key === "Enter") quickDownload(); });

async function quickDownload() {
  const location = $("#quick-input").value.trim();
  if (!location) { $("#quick-input").focus(); return; }
  const btn = $("#btn-quick");
  btn.disabled = true;
  $("#quick-hint").textContent = "Reading the link and queuing it…";
  try {
    await api("/api/playlist/import", {
      method: "POST",
      body: { source: "auto", location, mode: "audio", autostart: true },
    });
    $("#quick-input").value = "";
    toast("Downloading", "Reading the link — the download starts by itself.", "ok");
  } catch (e) {
    toast("Could not start", e.message, "error");
    $("#quick-hint").textContent = "Something went wrong — see the message above.";
  } finally {
    btn.disabled = false;
    setTimeout(() => {
      $("#quick-hint").innerHTML = "Paste a link and press <b>Download</b> — it imports and starts right away, using the folder and format saved in Settings.";
    }, 6000);
  }
}

/* ----------------------------------------------------------- tool setup */
const setupProgress = { file: 0, received: 0, at: 0, speed: 0 };

function resetSetupProgress() {
  setupProgress.file = 0;
  setupProgress.received = 0;
  setupProgress.at = 0;
  setupProgress.speed = 0;
  const wrap = $("#tools-progress");
  if (wrap) wrap.hidden = true;
  const fill = $("#tools-progress-fill");
  if (fill) fill.style.width = "0%";
  const pct = $("#tools-progress-pct");
  if (pct) pct.textContent = "0%";
  const text = $("#tools-progress-text");
  if (text) text.textContent = "Connecting…";
}

function renderToolsSetup(toolsInfo) {
  const card = $("#card-tools");
  if (!card || !toolsInfo) return;
  const setup = toolsInfo.setup || {};
  const missing = setup.needed || [];
  if (!missing.length) { card.hidden = true; return; }

  card.hidden = false;
  if (setup.auto_install) {
    $("#tools-hint").innerHTML =
      `This computer needs <b>${esc(missing.join(" and "))}</b> before audio can be ` +
      `extracted. One click installs them into the app's own folder (about 120 MB, ` +
      `once — they stay put after updates).`;
    const btn = $("#btn-tools-install");
    if (!btn.disabled) resetSetupProgress();
    btn.hidden = false;
    btn.textContent = "Install required tools";
  } else {
    $("#tools-hint").innerHTML =
      `This computer is missing <b>${esc(missing.join(" and "))}</b>. ` +
      `Install them, then restart the app:`;
    $("#btn-tools-install").hidden = true;
    $("#tools-status").innerHTML = "<code>brew install ffmpeg deno</code>";
    resetSetupProgress();
  }
}

$("#btn-tools-install")?.addEventListener("click", async () => {
  const btn = $("#btn-tools-install");
  btn.disabled = true;
  btn.textContent = "Starting…";
  resetSetupProgress();
  const wrap = $("#tools-progress");
  if (wrap) wrap.hidden = false;
  const text = $("#tools-progress-text");
  if (text) text.textContent = "Connecting…";
  try {
    await api("/api/tools/install", { method: "POST", body: {} });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Install required tools";
    resetSetupProgress();
    toast("Could not start setup", e.message, "error");
  }
});

/* ---------------------------------------------------------------- updates */
function parseChangelog(body) {
  if (!body) return [];
  const lines = body.split('\n');
  const items = [];
  for (let line of lines) {
    line = line.trim();
    if (line.match(/^[-*+]\s+/) || line.match(/^\d+\.\s+/)) {
      line = line.replace(/^[-*+]\s+/, '').replace(/^\d+\.\s+/, '');
      if (line && !line.startsWith('#')) items.push(line);
    }
  }
  return items.slice(0, 5);
}

function showUpdatePreview() {
  const u = state.update || {};
  if (!u.ok || !u.available) return;
  const items = parseChangelog(u.notes);
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.style.zIndex = '10000';
  modal.innerHTML = `
    <div class="modal-box" style="max-width: 500px;">
      <div class="modal-head">
        <b>🚀 What's coming in v${esc(u.latest)}</b>
        <button class="ghost" id="close-preview">✕</button>
      </div>
      <div class="modal-body">
        <p style="margin-top:0;color:var(--muted)">From your current v${esc(u.current)}</p>
        ${items.length > 0 ? `
          <div class="update-preview">
            <h4>✨ Key Features</h4>
            <ul>
              ${items.map(i => `<li>${esc(i)}</li>`).join('')}
            </ul>
          </div>
        ` : '<p style="color:var(--muted)">Check GitHub for full release notes.</p>'}
      </div>
      <div class="modal-foot">
        <button class="ghost" id="view-full">View full notes</button>
        <button class="update-btn-animated" id="preview-install" style="position:relative;overflow:hidden;padding:8px 16px;border-radius:8px;cursor:pointer;border:none">Got it, let's update</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  modal.querySelector('#close-preview').onclick = () => modal.remove();
  modal.querySelector('#view-full').onclick = () => { window.open(u.html_url, '_blank'); modal.remove(); };
  modal.querySelector('#preview-install').onclick = () => { modal.remove(); showUpdateWarning(); };
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
}

function showUpdateWarning() {
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.style.zIndex = '10000';
  modal.innerHTML = `
    <div class="modal-box" style="max-width: 480px;">
      <div class="modal-head">
        <b>⚠️ Before you update</b>
      </div>
      <div class="modal-body">
        <p style="margin-top:0">Please read these important notes:</p>
        <ul style="margin:12px 0;padding-left:20px;line-height:1.8">
          <li><strong>Don't close the app</strong> - It will restart automatically</li>
          <li><strong>Downloads may pause briefly</strong> - Any active downloads will resume after restart</li>
          <li><strong>Keep your files safe</strong> - Your downloads and settings won't be affected</li>
          <li><strong>May take a moment</strong> - The app will disappear and reopen in a few seconds</li>
        </ul>
        <p style="color:var(--warn);font-size:12px">Make sure you're not in the middle of important work.</p>
      </div>
      <div class="modal-foot">
        <button class="ghost" id="warn-cancel">Cancel</button>
        <button class="update-btn-animated" id="warn-continue" style="position:relative;overflow:hidden;padding:8px 16px;border-radius:8px;cursor:pointer;border:none">I understand, continue</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  modal.querySelector('#warn-cancel').onclick = () => modal.remove();
  modal.querySelector('#warn-continue').onclick = () => { modal.remove(); startUpdate(); };
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
}

function showUpdateAnimation() {
  const overlay = document.createElement('div');
  overlay.className = 'update-overlay active';
  overlay.innerHTML = `
    <div class="update-overlay-particles" id="update-particles"></div>
    <div class="update-overlay-content">
      <div class="update-spinner"></div>
      <div class="update-message">Updating Playlist Downloader</div>
      <div class="update-submessage" id="update-anim-msg">Downloading the latest version...</div>
    </div>
  `;
  document.body.appendChild(overlay);
  const particles = overlay.querySelector('#update-particles');
  for (let i = 0; i < 30; i++) {
    const p = document.createElement('div');
    p.className = 'update-particle';
    p.style.left = Math.random() * 100 + '%';
    p.style.animationDuration = (Math.random() * 3 + 2) + 's';
    p.style.animationDelay = Math.random() * 2 + 's';
    particles.appendChild(p);
  }
  const msg = overlay.querySelector('#update-anim-msg');
  const msgs = ['Downloading the latest version...', 'Preparing installation...', 'Swapping in new version...', 'Almost ready...', 'Restarting...'];
  let i = 0;
  const iv = setInterval(() => { i = (i + 1) % msgs.length; msg.textContent = msgs[i]; }, 1500);
  overlay._iv = iv;
  return overlay;
}

async function startUpdate() {
  const u = state.update || {};
  const btn = $("#btn-update-install");
  const overlay = showUpdateAnimation();
  try {
    if (!u.asset) {
      overlay.remove();
      if (u.html_url) window.open(u.html_url, "_blank");
      toast("Manual update", "Opening the download page for this version.", "warn");
      return;
    }
    if (btn) { btn.disabled = true; btn.textContent = "Updating..."; }
    await api("/api/update/install", { method: "POST", body: { url: u.asset.url } });
    setTimeout(() => {
      overlay.querySelector('.update-message').textContent = 'Update Complete!';
      overlay.querySelector('.update-submessage').textContent = 'The app will restart momentarily...';
    }, 2000);
    $("#update-banner").hidden = true;
  } catch (e) {
    if (overlay._iv) clearInterval(overlay._iv);
    overlay.remove();
    if (btn) { btn.disabled = false; btn.textContent = "⚡ Update & restart"; }
    toast("Update failed", e.message, "error");
    checkForUpdate(false);
  }
}

function renderUpdate() {
  const u = state.update || {};
  if (u.current) $("#update-current").textContent = "v" + u.current;
  const status = $("#update-status");
  const banner = $("#update-banner");
  if (!status) return;
  if (!u.ok) {
    status.textContent = u.error ? "Could not check: " + u.error : "Not checked yet.";
    banner.hidden = true;
    return;
  }
  if (u.available) {
    status.innerHTML = `Update <b>v${esc(u.latest)}</b> is available.`;
    $("#update-banner-text").innerHTML = `Version <b>v${esc(u.latest)}</b> is available.`;
    banner.hidden = false;
  } else {
    status.textContent = "You're up to date (v" + u.current + ").";
    banner.hidden = true;
  }
}

async function checkForUpdate(force) {
  const btn = $("#btn-check-update");
  if (btn) { btn.disabled = true; btn.textContent = "Checking…"; }
  try {
    const r = await api(`/api/update/check${force ? "?force=1" : ""}`);
    state.update = r;
    renderUpdate();
    if (force && r.ok && !r.available) toast("Up to date", `You're on v${r.current}.`, "ok");
    if (force && !r.ok) toast("Could not check", r.error || "Try again later.", "warn");
  } catch (e) {
    if (btn) $("#update-status").textContent = "Could not check: " + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Check for updates"; }
  }
}

$("#btn-check-update")?.addEventListener("click", () => checkForUpdate(true));
$("#btn-update-dismiss")?.addEventListener("click", () => { $("#update-banner").hidden = true; });
$("#btn-update-preview")?.addEventListener("click", showUpdatePreview);
$("#btn-update-install")?.addEventListener("click", () => {
  const u = state.update || {};
  if (!u.asset) {
    if (u.html_url) window.open(u.html_url, "_blank");
    toast("Manual update", "Opening the download page for this version.", "warn");
    return;
  }
  showUpdateWarning();
});


$("#btn-resume").addEventListener("click", () => api("/api/download/resume", { method: "POST" }));
$("#btn-stop").addEventListener("click", () => api("/api/download/stop", { method: "POST" }));
$("#btn-retry").addEventListener("click", async () => {
  try {
    const r = await api("/api/items/retry-failed", {
      method: "POST", body: { playlist_id: state.currentPlaylist },
    });
    toast("Re-queued", `${r.requeued} failed items are pending again.`, "ok");
    await loadItems();
  } catch (e) { toast("Nothing to retry", e.message, "warn"); }
});

$("#btn-quit").addEventListener("click", async () => {
  const running = +$("#stat-active").textContent;
  const warn = running ? "\n\nA download is still running — it will be stopped." : "";
  if (!confirm(`Quit Playlist Downloader?${warn}\n\nYour queue is saved, so you can pick up where you left off.`)) return;
  $("#btn-quit").disabled = true;
  try { await api("/api/shutdown", { method: "POST" }); } catch { /* the server is going away */ }
  await new Promise(r => setTimeout(r, 800));
  document.body.innerHTML = `
    <div class="quit-screen">
      <div class="card">
        <h2>Playlist Downloader has quit</h2>
        <p class="hint">The background service has stopped and your queue was saved.
        Close this tab, or click the app icon to start it again.</p>
        <button class="primary" onclick="window.close()">Close this tab</button>
      </div>
    </div>`;
});

function setRunUI() {
  const { running, paused } = state.run;
  $("#btn-start").hidden = running;
  $("#btn-pause").hidden = !running || paused;
  $("#btn-resume").hidden = !running || !paused;
  $("#btn-stop").hidden = !running;
  updateStats({
    total: +$("#stat-total").textContent,
    done: +$("#stat-done").textContent,
    pending: +$("#stat-pending").textContent,
    failed: +$("#stat-failed").textContent,
  });
}

/* ----------------------------------------------------------------- events */
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => { $("#conn").className = "conn live"; $("#conn").textContent = "live"; };
  es.onerror = () => {
    $("#conn").className = "conn down";
    $("#conn").textContent = "reconnecting…";
  };
  es.onmessage = ev => {
    let e;
    try { e = JSON.parse(ev.data); } catch { return; }
    handleEvent(e.type, e.data);
  };
}

function handleEvent(type, d) {
  switch (type) {
    case "item_started": {
      state.live.set(d.id, { progress: 0, stage: "downloading" });
      const it = state.items.find(x => x.id === d.id);
      if (it) { it.status = "downloading"; it.progress = 0; }
      const tr = state.queue.get(d.id);
      if (tr) { tr.className = "downloading"; const pill = $(".pill", tr); if (pill) { pill.className = "pill downloading"; pill.textContent = "downloading"; } }
      bumpStat("active", 1);
      break;
    }
    case "item_progress": {
      state.live.set(d.id, d);
      applyProgress(d.id, d);
      break;
    }
    case "item_stage": {
      const p = state.live.get(d.id) || {};
      p.stage = d.stage;
      state.live.set(d.id, p);
      const tr = state.queue.get(d.id);
      if (tr) { const pill = $(".pill", tr); if (pill) { pill.className = "pill downloading"; pill.textContent = d.stage; } }
      break;
    }
    case "item_done": {
      state.live.delete(d.id);
      bumpStat("active", -1);
      const it = state.items.find(x => x.id === d.id);
      const skipped = !!d.skipped;
      if (it) {
        it.status = skipped ? "skipped" : "done";
        it.progress = 100;
        it.filepath = d.filepath;
      }
      const tr = state.queue.get(d.id);
      if (tr) {
        tr.className = skipped ? "skipped" : "done";
        const pill = $(".pill", tr);
        if (pill) {
          pill.className = `pill ${skipped ? "skipped" : "done"}`;
          pill.textContent = skipped ? "skipped" : "done";
        }
        const cell = $(".col-progress", tr);
        if (cell) {
          cell.innerHTML = skipped
            ? `<span class="meta" title="${esc(d.reason || "")}">${esc(d.reason || "already have it")}</span>`
            : `<div class="progress">${bar(100, "done")}<span class="meta">done</span></div>`;
        }
      }
      if (skipped) bumpStat("skipped", 1); else bumpStat("done", 1);
      bumpStat("pending", -1);
      break;
    }
    case "item_retrying": {
      state.live.set(d.id, { progress: 0, stage: `retry ${d.attempt}/${d.of}` });
      const tr = state.queue.get(d.id);
      if (tr) {
        const pill = $(".pill", tr);
        if (pill) { pill.className = "pill downloading"; pill.textContent = `retry ${d.attempt}`; }
        const cell = $(".col-progress", tr);
        if (cell) cell.innerHTML = `<span class="meta" style="color:var(--warn)">retrying in ${d.delay}s (${esc((d.error || "").slice(0, 30))})</span>`;
      }
      break;
    }
    case "item_failed": {
      state.live.delete(d.id);
      bumpStat("active", -1);
      const it = state.items.find(x => x.id === d.id);
      if (it) { it.status = "failed"; it.error = d.error; }
      const tr = state.queue.get(d.id);
      if (tr) {
        tr.className = "failed";
        const pill = $(".pill", tr); if (pill) { pill.className = "pill failed"; pill.textContent = "failed"; }
        const cell = $(".col-progress", tr);
        if (cell) cell.innerHTML = `<span class="meta" style="color:var(--bad)" title="${esc(d.error)}">${esc((d.error || "").slice(0, 46))}</span>`;
      }
      bumpStat("failed", 1); bumpStat("pending", -1);
      logLine("error", `${d.title || "item"}: ${d.error}`);
      break;
    }
    case "run_started":
      state.run.running = true; state.run.paused = false; setRunUI();
      toast("Running", `${d.queued} items · ${d.workers} workers`, "ok");
      break;
    case "run_paused": state.run.paused = true; setRunUI(); break;
    case "run_resumed": state.run.paused = false; setRunUI(); break;
    case "run_stopping": state.run.running = false; setRunUI(); break;
    case "run_finished":
      state.run.running = false; state.run.paused = false;
      state.live.clear(); setRunUI();
      toast("Finished", `${d.stats.done} done · ${d.stats.failed} failed`, d.stats.failed ? "warn" : "ok");
      refreshAll();
      break;
    case "playlists_changed": loadPlaylists(); break;
    case "import_progress": {
      $("#import-hint").textContent = d.message || "";
      if (d.stage === "done") {
        $("#btn-import").disabled = false;
        $("#import-input").value = "";
        $("#import-hint").textContent = "";
        toast("Imported", d.message, "ok");
        if (d.warning) toast("Heads up", d.warning, "warn");
        loadPlaylists().then(() => { if (d.job) refreshPlaylistProgress(); });
        setTimeout(() => { if (state.currentPlaylist == null && state.playlists[0]) selectPlaylist(state.playlists[0].id); }, 400);
      } else if (d.stage === "error") {
        $("#btn-import").disabled = false;
        $("#import-hint").textContent = "";
      }
      break;
    }
    case "compare_progress":
      $("#compare-status").textContent = d.message || "";
      $("#btn-compare").disabled = d.stage !== "done" && d.stage !== "error";
      break;
    case "update_progress": {
      const btn = $("#btn-update-install");
      if (d.stage === "downloading" && d.total) {
        const pct = Math.round((d.received / d.total) * 100);
        if (btn) btn.textContent = `Downloading ${pct}%`;
      } else if (d.stage === "preparing") {
        if (btn) btn.textContent = "Installing…";
      } else if (d.stage === "restarting") {
        if (btn) btn.textContent = "Restarting…";
      }
      break;
    }
    case "tools_progress": {
      const btn = $("#btn-tools-install");
      const status = $("#tools-status");
      const wrap = $("#tools-progress");
      const fill = $("#tools-progress-fill");
      const pctEl = $("#tools-progress-pct");
      const text = $("#tools-progress-text");
      const files = Math.max(1, d.files || 1);
      const file = Math.max(1, d.file || 1);

      if (d.stage === "starting" || d.stage === "downloading" || d.stage === "unpacking") {
        if (wrap) wrap.hidden = false;
        if (btn) btn.disabled = true;
      }

      if (d.stage === "starting") {
        setupProgress.file = file;
        setupProgress.received = 0;
        setupProgress.at = 0;
        setupProgress.speed = 0;
        const raw = (file - 1) / files;
        const shown = 1 - Math.pow(1 - raw, 2);
        const pct = Math.round(shown * 100);
        if (btn) btn.textContent = `Preparing ${d.tool || "tools"}…`;
        if (text) text.textContent = `Connecting to the ${d.tool || "tool"} download…`;
        if (fill) fill.style.width = `${pct}%`;
        if (pctEl) pctEl.textContent = `${pct}%`;
      } else if (d.stage === "downloading") {
        if (setupProgress.file !== file) {
          setupProgress.file = file;
          setupProgress.received = 0;
          setupProgress.at = 0;
          setupProgress.speed = 0;
        }
        // Bytes across all files, so the bar covers the whole setup, not just
        // the current download. Ease-out makes early progress move quickly and
        // the final stretch slow down, while still ending at exactly 100%.
        const part = d.total ? Math.min(1, d.received / d.total) : 0;
        const raw = (file - 1 + part) / files;
        const shown = 1 - Math.pow(1 - raw, 2);
        const pct = Math.round(shown * 100);
        if (fill) fill.style.width = `${pct}%`;
        if (pctEl) pctEl.textContent = `${pct}%`;

        const now = performance.now();
        if (setupProgress.at && now - setupProgress.at >= 250) {
          const dt = (now - setupProgress.at) / 1000;
          const instant = Math.max(0, d.received - setupProgress.received) / dt;
          setupProgress.speed = setupProgress.speed
            ? setupProgress.speed * 0.7 + instant * 0.3
            : instant;
          setupProgress.received = d.received;
          setupProgress.at = now;
        } else if (!setupProgress.at) {
          setupProgress.received = d.received;
          setupProgress.at = now;
        }

        if (btn) btn.textContent = `Installing ${d.tool || "tools"}…`;
        if (text) {
          const amount = d.total
            ? `${fmtBytes(d.received)} of ${fmtBytes(d.total)}`
            : fmtBytes(d.received);
          const rate = setupProgress.speed > 0 ? ` · ${fmtBytes(setupProgress.speed)}/s` : "";
          text.textContent = `${d.tool || "Tools"}${files > 1 ? ` (${file} of ${files})` : ""} — ${amount}${rate}`;
        }
      } else if (d.stage === "unpacking") {
        const raw = file / files;
        const shown = 1 - Math.pow(1 - raw, 2);
        const pct = Math.round(shown * 100);
        if (fill) fill.style.width = `${pct}%`;
        if (pctEl) pctEl.textContent = `${pct}%`;
        if (btn) btn.textContent = `Unpacking ${d.tool || "tools"}…`;
        if (text) text.textContent = `Unpacking ${d.tool || "tools"}…`;
      } else if (d.stage === "done") {
        if (fill) fill.style.width = "100%";
        if (pctEl) pctEl.textContent = "100%";
        if (text) text.textContent = "Finished — the tools are ready.";
        toast("Setup finished", d.message || "Everything is ready.", "ok");
        setTimeout(() => {
          $("#card-tools").hidden = true;
          resetSetupProgress();
          loadState();
        }, 900);
      } else if (d.stage === "error") {
        resetSetupProgress();
        if (btn) { btn.disabled = false; btn.textContent = "Try again"; }
        if (status) status.textContent = "";
        toast("Setup failed", d.error || "Check your internet connection and try again.", "error");
      }
      break;
    }
    case "log": logLine(d.level, d.message); break;
  }
}

async function refreshPlaylistProgress() {
  await loadPlaylists();
}

function bumpStat(key, delta) {
  const map = {
    done: "#stat-done", pending: "#stat-pending", failed: "#stat-failed",
    active: "#stat-active", skipped: "#stat-skipped",
  };
  const el = $(map[key]);
  if (el) el.textContent = Math.max(0, (+el.textContent || 0) + delta);
}

async function refreshAll() {
  const s = await api("/api/state");
  state.run = s.run;
  updateStats(s.stats);
  setRunUI();
  await loadPlaylists();
  if (state.currentPlaylist) await loadItems();
}

/* -------------------------------------------------------------------- log */
const LOG_MAX = 500;
function logLine(level, message) {
  const view = $("#log-view");
  const el = document.createElement("div");
  el.className = `log-line ${level}`;
  const t = new Date().toLocaleTimeString();
  el.innerHTML = `<time>${t}</time><span>${esc(message)}</span>`;
  view.appendChild(el);
  while (view.childElementCount > LOG_MAX) view.removeChild(view.firstChild);
  if (view.scrollHeight - view.scrollTop - view.clientHeight < 120) view.scrollTop = view.scrollHeight;
}
$("#btn-clear-log").addEventListener("click", () => $("#log-view").replaceChildren());

/* ---------------------------------------------------------------- compare */
let comparePoll = null;

$("#compare-threshold").addEventListener("input", e => {
  const v = Number(e.target.value);
  $("#threshold-label").textContent = v <= 0.6 ? "loose" : v >= 0.85 ? "strict" : "balanced";
});

function renderComparePlaylists() {
  const sel = $("#compare-playlist");
  const cur = sel.value;
  sel.innerHTML = `<option value="">— choose —</option>` + state.playlists.map(p =>
    `<option value="${p.id}">${esc(p.name)} (${p.total})</option>`).join("");
  if (cur) sel.value = cur;
}

$("#compare-ignore-extra").addEventListener("change", () => {
  if (!state.lastCompare) return;
  renderCompare(state.lastCompare, $("#compare-ignore-extra").checked);
});

$("#btn-compare").addEventListener("click", async () => {
  const playlist_id = $("#compare-playlist").value;
  const folder = $("#compare-folder").value.trim();
  if (!playlist_id) { toast("Pick a playlist", "Choose which playlist to check.", "warn"); return; }
  if (!folder) { toast("Pick a folder", "Choose the folder to scan.", "warn"); return; }

  $("#btn-compare").disabled = true;
  $("#compare-status").textContent = "Starting…";
  try {
    const { job } = await api("/api/compare", {
      method: "POST",
      body: {
        playlist_id: Number(playlist_id),
        folder,
        threshold: Number($("#compare-threshold").value),
        recursive: $("#compare-recursive").checked,
        read_tags: $("#compare-read-tags").checked,
      },
    });
    pollJob(job);
  } catch (e) {
    $("#btn-compare").disabled = false;
    $("#compare-status").textContent = "";
    toast("Compare failed", e.message, "error");
  }
});

function pollJob(id) {
  clearInterval(comparePoll);
  comparePoll = setInterval(async () => {
    let job;
    try { ({ job } = await api(`/api/job/${id}`)); }
    catch { clearInterval(comparePoll); return; }
    if (job.status === "running") return;
    clearInterval(comparePoll);
    $("#btn-compare").disabled = false;
    if (job.status === "error") {
      $("#compare-status").textContent = "";
      toast("Compare failed", job.error, "error");
      return;
    }
    state.lastCompare = job.result;
    renderCompare(job.result, $("#compare-ignore-extra").checked);
    $("#compare-status").textContent = "";
    toast("Comparison complete",
      `${job.result.counts.missing} missing of ${job.result.counts.playlist} tracks`,
      job.result.counts.missing ? "warn" : "ok");
  }, 600);
}

function renderCompare(r, ignoreExtra) {
  $("#compare-results").hidden = false;
  const c = r.counts;
  $("#compare-summary").innerHTML = `
    <div class="summary-chip"><b>${c.playlist}</b>in playlist</div>
    <div class="summary-chip"><b>${c.folder}</b>files on disk</div>
    <div class="summary-chip missing"><b>${c.missing}</b>missing</div>
    <div class="summary-chip matched"><b>${c.matched}</b>matched</div>
    <div class="summary-chip extra"><b>${c.extra}</b>extra</div>`;

  $("#missing-table tbody").innerHTML = r.missing.length ? r.missing.map((m, i) => `
    <tr>
      <td class="col-num">${i + 1}</td>
      <td><div class="cell-title" title="${esc(m.title)}">${esc(m.title)}</div></td>
      <td>${esc(m.artist || "")}</td>
      <td><div class="cell-file" title="${esc((m.closest || {}).file || "")}">${
        m.closest ? `${esc(m.closest.name)} <span class="muted">(${m.closest.score})</span>` : "<span class='muted'>—</span>"
      }</div></td>
    </tr>`).join("") : `<tr><td colspan="4" class="empty">🎉 Nothing missing — every playlist track is on disk.</td></tr>`;

  $("#matched-table tbody").innerHTML = r.matched.length ? r.matched.map((m, i) => `
    <tr>
      <td class="col-num">${i + 1}</td>
      <td><div class="cell-title">${esc(m.artist ? m.artist + " - " + m.title : m.title)}</div></td>
      <td><div class="cell-file" title="${esc(m.file)}">${esc(m.file_name)}</div></td>
      <td class="col-score"><span class="pill ${m.score > 0.9 ? "done" : ""}">${m.score}</span></td>
    </tr>`).join("") : `<tr><td colspan="4" class="empty">No matches yet.</td></tr>`;

  $("#extra-table tbody").innerHTML = r.extra.length ? r.extra.map((m, i) => `
    <tr>
      <td class="col-num">${i + 1}</td>
      <td><div class="cell-file" title="${esc(m.file)}">${esc(m.name)}</div></td>
      <td>${esc(m.artist || "")}</td>
      <td>${esc(m.album || "")}</td>
    </tr>`).join("") : `<tr><td colspan="4" class="empty">No extra files.</td></tr>`;
}

$("#btn-export-missing").addEventListener("click", () => {
  const r = state.lastCompare;
  if (!r || !r.missing.length) { toast("Nothing to export", "", "warn"); return; }
  const lines = r.missing.map(m => m.artist ? `${m.artist} - ${m.title}` : m.title);
  const blob = new Blob([lines.join("\n")], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `missing-from-${r.label.replace(/\W+/g, "-").slice(0, 40)}.txt`;
  a.click();
  URL.revokeObjectURL(a.href);
});

$("#btn-import-missing").addEventListener("click", async () => {
  const r = state.lastCompare;
  if (!r || !r.missing.length) { toast("Nothing missing", "", "warn"); return; }
  try {
    const res = await api("/api/compare/import-missing", {
      method: "POST",
      body: {
        name: `Missing from ${r.label}`.slice(0, 90),
        items: r.missing.map(m => ({
          title: m.title, artist: m.artist, album: m.album,
          duration: m.duration, url: m.url,
        })),
      },
    });
    toast("Added to queue", `${res.added} tracks in a new playlist.`, "ok");
    await loadPlaylists();
    await selectPlaylist(res.playlist_id);
    $$(".tab").find(t => t.dataset.tab === "download")?.click();
  } catch (e) { toast("Could not add", e.message, "error"); }
});

/* ------------------------------------------------------------ folder popup */
$("#btn-browse").addEventListener("click", async () => {
  const start = $("#compare-folder").value || "";
  await openBrowser(start, path => { $("#compare-folder").value = path; closeModal(); });
});

async function openBrowser(path, onPick) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  state.browse = data;
  $("#modal-path").textContent = data.path;
  $("#modal-list").innerHTML = data.entries.length
    ? data.entries.map(e => `<div class="modal-item" data-path="${esc(e.path)}">📁 ${esc(e.name)}</div>`).join("")
    : `<p class="empty">No sub-folders here.</p>`;
  $$("#modal-list .modal-item").forEach(el => el.addEventListener("click", () => openBrowser(el.dataset.path, onPick)));
  $("#modal-up").onclick = () => openBrowser(data.parent, onPick);
  $("#modal-pick").onclick = () => onPick(data.path);
  $("#modal").hidden = false;
}
function closeModal() { $("#modal").hidden = true; }
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });

/* --------------------------------------------------------------- kick-off */
(async function init() {
  try {
    // auth.js gates the app when accounts are configured; without a backend
    // this resolves immediately and nothing below changes.
    if (window.authReady) await window.authReady;
    await loadState();
    setRunUI();
    connectEvents();
    logLine("info", "Ready. Import a playlist to begin.");
    if (state.settings && state.settings.auto_check_updates !== false) {
      checkForUpdate(false).catch(() => {});
    }
  } catch (e) {
    toast("Could not reach the app", e.message, "error");
    $("#conn").className = "conn down";
    $("#conn").textContent = "offline";
  }
})();
