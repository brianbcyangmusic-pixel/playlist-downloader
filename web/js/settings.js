/* Settings tab: fill, track unsaved changes, save. */

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
const RANGE_LABELS = {
  concurrent_downloads: "#lbl-workers",
  concurrent_fragments: "#lbl-frags",
  item_retries: "#lbl-item-retries",
};

function fillSettings(s) {
  TEXT_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) el.value = s[k] ?? ""; });
  BOOL_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) el.checked = !!s[k]; });
  RANGE_KEYS.forEach(k => {
    const el = $(`#set-${k}`);
    if (el) el.value = s[k];
    $(RANGE_LABELS[k]).textContent = s[k];
  });
  setDirty(false);
}

function readSettings() {
  const body = {};
  TEXT_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) body[k] = el.value; });
  BOOL_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) body[k] = el.checked; });
  RANGE_KEYS.forEach(k => { const el = $(`#set-${k}`); if (el) body[k] = Number(el.value); });
  return body;
}

function setDirty(dirty) {
  $("#save-bar").classList.toggle("dirty", dirty);
  $("#save-state").textContent = dirty ? "Unsaved changes" : "All changes saved";
  $("#btn-discard").hidden = !dirty;
}

async function saveSettings() {
  const btn = $("#btn-save");
  btn.disabled = true;
  try {
    const res = await api("/api/settings", { method: "POST", body: readSettings() });
    state.settings = res.settings;
    renderTools(res.tools);
    setDirty(false);
    toast("Settings saved", "", "ok");
  } catch (e) {
    toast("Could not save", e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

function renderTools(t) {
  const js = (t.js_runtimes && t.js_runtimes.length) ? t.js_runtimes.join("/") : "";
  const tool = (ok, name, label) =>
    `<span class="tool"><i class="dot ${ok ? "ok" : ""}"></i>${name} <b>${label}</b></span>`;
  $("#tool-status").innerHTML = [
    tool(t.ytdlp_ok, "yt-dlp", t.ytdlp_ok ? "ready" : "missing"),
    tool(t.ffmpeg_ok, "ffmpeg", t.ffmpeg_ok ? "ready" : "missing"),
    tool(t.ejs_ok, "JS challenge", t.ejs_ok ? (js || "ready") : "missing"),
    tool(t.rapidfuzz_installed, "fast match", t.rapidfuzz_installed ? "ready" : "slow"),
  ].join("");
}

const settingsForm = $("#settings-form");
settingsForm.addEventListener("input", e => {
  const key = e.target.id.replace(/^set-/, "");
  if (RANGE_LABELS[key]) $(RANGE_LABELS[key]).textContent = e.target.value;
  setDirty(true);
});
settingsForm.addEventListener("change", () => setDirty(true));
settingsForm.addEventListener("submit", e => { e.preventDefault(); saveSettings(); });
$("#btn-discard").addEventListener("click", () => fillSettings(state.settings));
document.addEventListener("keydown", e => {
  const inSettings = $("#tab-settings").classList.contains("active");
  if (inSettings && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    saveSettings();
  }
});

$("#btn-reveal").addEventListener("click", async () => {
  try { await api("/api/reveal", { method: "POST", body: { path: $("#set-download_dir").value } }); }
  catch (e) { toast("Could not open folder", e.message, "error"); }
});
