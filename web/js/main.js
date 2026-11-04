/* Boot: load state, wire the quit button, open the event stream. */

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

$("#btn-quit").addEventListener("click", async () => {
  const running = state.run.running;
  const ok = await confirmDialog({
    title: "Quit Playlist Downloader?",
    body: (running ? "<p><b>A download is running — it will be stopped.</b></p>" : "") +
      "<p>Your queue is saved, so you can pick up where you left off.</p>",
    ok: "Quit", danger: running,
  });
  if (!ok) return;
  $("#btn-quit").disabled = true;
  try { await api("/api/shutdown", { method: "POST" }); } catch { /* the server is going away */ }
  await new Promise(r => setTimeout(r, 800));
  document.body.innerHTML = `
    <div class="quit-screen">
      <div class="card">
        <span class="logo">${icon("power")}</span>
        <h2>Playlist Downloader has quit</h2>
        <p class="hint">The background service has stopped and your queue was saved.
        Close this tab, or click the app icon to start it again.</p>
        <button class="primary" onclick="window.close()">Close this tab</button>
      </div>
    </div>`;
});

(async function init() {
  renderPlaylists();
  renderQueue();
  try {
    // auth.js gates the app when accounts are configured; without a backend
    // this resolves immediately and nothing below changes.
    if (window.authReady) await window.authReady;
    await loadState();
    setRunUI();
    connectEvents();
    logLine("info", "Ready. Paste a playlist link to begin.");
    if (state.playlists.length && !state.currentPlaylist) selectPlaylist(state.playlists[0].id);
    if (state.settings && state.settings.auto_check_updates !== false) {
      checkForUpdate(false).catch(() => {});
    }
  } catch (e) {
    toast("Could not reach the app", e.message, "error");
    $("#conn").className = "conn down";
    $("#conn").textContent = "offline";
  }
})();
