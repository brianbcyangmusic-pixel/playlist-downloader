/* Playlist Downloader — shared state, helpers and dialogs.
 *
 * The front end is plain scripts (no build step) loaded in order by
 * index.html; top-level declarations here are shared with the later files.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  settings: {},
  defaults: {},
  tools: {},
  playlists: [],
  currentPlaylist: null,
  items: [],
  statusFilter: "",
  run: { running: false, paused: false, active: [] },
  queue: new Map(),          // item id -> row element
  live: new Map(),           // item id -> live progress payload
  lastCompare: null,
  update: {},
};

const TOAST_MS = { error: 7000, default: 3800 };

/* ------------------------------------------------------------------ utils */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function icon(name) {
  return `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtTime(s) {
  if (s === null || s === undefined) return "";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = n => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function bar(pct, cls = "") {
  return `<div class="track"><i class="${cls}" style="width:${Math.min(100, pct || 0)}%"></i></div>`;
}

function emptyState(iconName, title, body = "") {
  return `<div class="empty-state">${icon(iconName)}<b>${esc(title)}</b>${body ? `<p>${body}</p>` : ""}</div>`;
}

function toast(title, body = "", kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.innerHTML = `<b>${esc(title)}</b>${body ? `<span>${esc(body)}</span>` : ""}`;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 320);
  }, TOAST_MS[kind] || TOAST_MS.default);
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

/* ---------------------------------------------------------------- modals */
// Every modal closes on Esc and on a click on its backdrop; `onClose` hooks
// let a caller react (the confirm dialog resolves its promise to false).
const modalClosers = new Map();

function openModal(id, onClose) {
  const el = document.getElementById(id);
  el.hidden = false;
  modalClosers.set(id, onClose || null);
  const focusable = el.querySelector("input, .primary, button");
  if (focusable) setTimeout(() => focusable.focus(), 30);
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (!el || el.hidden) return;
  el.hidden = true;
  const cb = modalClosers.get(id);
  modalClosers.delete(id);
  if (cb) cb();
}

$$(".modal").forEach(m => m.addEventListener("click", e => {
  if (e.target === m) closeModal(m.id);
}));
document.addEventListener("keydown", e => {
  if (e.key !== "Escape" || !modalClosers.size) return;
  const ids = [...modalClosers.keys()];
  closeModal(ids[ids.length - 1]);
});

/**
 * Styled replacement for window.confirm().
 * @param {{title: string, body?: string, ok?: string, danger?: boolean}} opts
 * @returns {Promise<boolean>}
 */
function confirmDialog({ title, body = "", ok = "OK", danger = false }) {
  return new Promise(resolve => {
    $("#confirm-title").textContent = title;
    $("#confirm-body").innerHTML = body;
    const okBtn = $("#confirm-ok");
    okBtn.textContent = ok;
    okBtn.className = danger ? "danger" : "primary";
    let answered = false;
    const finish = value => {
      if (answered) return;
      answered = true;
      closeModal("confirm-modal");
      resolve(value);
    };
    okBtn.onclick = () => finish(true);
    $("#confirm-cancel").onclick = () => finish(false);
    openModal("confirm-modal", () => finish(false));
    setTimeout(() => okBtn.focus(), 40);
  });
}

/* ----------------------------------------------------------- folder picker */
async function openBrowser(path, onPick) {
  let data;
  try {
    data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  } catch (e) {
    toast("Could not open folder", e.message, "error");
    return;
  }
  $("#modal-path").textContent = data.path;
  $("#modal-list").innerHTML = data.entries.length
    ? data.entries.map(e =>
        `<button class="modal-item" data-path="${esc(e.path)}">${icon("folder")}${esc(e.name)}</button>`).join("")
    : emptyState("folder", "No sub-folders here");
  $$("#modal-list .modal-item").forEach(el =>
    el.addEventListener("click", () => openBrowser(el.dataset.path, onPick)));
  $("#modal-up").onclick = () => openBrowser(data.parent, onPick);
  $("#modal-pick").onclick = () => { onPick(data.path); closeModal("modal"); };
  if ($("#modal").hidden) openModal("modal");
}
$("#modal-close").addEventListener("click", () => closeModal("modal"));

/* -------------------------------------------------------------------- nav */
function showTab(name) {
  $$(".tab").forEach(t => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    if (on) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current");
  });
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "log") $("#log-badge").hidden = true;
}
$$(".tab").forEach(tab => tab.addEventListener("click", () => showTab(tab.dataset.tab)));
