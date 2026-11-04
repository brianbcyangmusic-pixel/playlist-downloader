/* App updates: check, banner, release notes, install with restart overlay. */

const MAX_NOTES = 6;
const OVERLAY_STEPS = ["Downloading the latest version…", "Preparing installation…", "Swapping in the new version…", "Restarting…"];

function parseChangelog(body) {
  if (!body) return [];
  return body.split("\n")
    .map(line => line.trim())
    .filter(line => /^([-*+]|\d+\.)\s+/.test(line))
    .map(line => line.replace(/^([-*+]|\d+\.)\s+/, ""))
    .filter(line => line && !line.startsWith("#"))
    .slice(0, MAX_NOTES);
}

function renderUpdate() {
  const u = state.update || {};
  if (u.current) $("#update-current").textContent = `v${u.current}`;
  const status = $("#update-status");
  const banner = $("#update-banner");
  if (!u.ok) {
    status.textContent = u.error ? `Could not check: ${u.error}` : "Not checked yet.";
    banner.hidden = true;
    return;
  }
  if (u.available) {
    status.innerHTML = `Version <b>v${esc(u.latest)}</b> is available.`;
    $("#update-banner-text").innerHTML = `Version <b>v${esc(u.latest)}</b> is ready to install.`;
    banner.hidden = false;
  } else {
    status.textContent = `You're up to date (v${u.current}).`;
    banner.hidden = true;
  }
}

async function checkForUpdate(force) {
  const btn = $("#btn-check-update");
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const r = await api(`/api/update/check${force ? "?force=1" : ""}`);
    state.update = r;
    renderUpdate();
    if (force && r.ok && !r.available) toast("Up to date", `You're on v${r.current}.`, "ok");
    if (force && !r.ok) toast("Could not check", r.error || "Try again later.", "warn");
  } catch (e) {
    $("#update-status").textContent = `Could not check: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Check for updates";
  }
}

function openUpdateDialog() {
  const u = state.update || {};
  if (!u.ok || !u.available) return;
  const notes = parseChangelog(u.notes);
  $("#update-title").textContent = `What's new in v${u.latest}`;
  $("#update-from").textContent = `You have v${u.current}.`;
  $("#update-notes").innerHTML = notes.length
    ? notes.map(n => `<li>${esc(n)}</li>`).join("")
    : `<li>See the full release notes for details.</li>`;
  $("#update-full").hidden = !u.html_url;
  openModal("update-modal");
}

function showUpdateOverlay() {
  const overlay = $("#update-overlay");
  overlay.hidden = false;
  let i = 0;
  $("#update-overlay-msg").textContent = OVERLAY_STEPS[0];
  const timer = setInterval(() => {
    i = Math.min(i + 1, OVERLAY_STEPS.length - 1);
    $("#update-overlay-msg").textContent = OVERLAY_STEPS[i];
  }, 1800);
  return () => { clearInterval(timer); overlay.hidden = true; };
}

async function startUpdate() {
  const u = state.update || {};
  closeModal("update-modal");
  if (!u.asset) {
    if (u.html_url) window.open(u.html_url, "_blank");
    toast("Manual update", "Opening the download page for this version.", "warn");
    return;
  }
  const hideOverlay = showUpdateOverlay();
  try {
    await api("/api/update/install", { method: "POST", body: { url: u.asset.url } });
    $("#update-banner").hidden = true;
  } catch (e) {
    hideOverlay();
    toast("Update failed", e.message, "error");
    checkForUpdate(false);
  }
}

function onUpdateProgress(d) {
  const msg = $("#update-overlay-msg");
  if (d.stage === "downloading" && d.total) {
    msg.textContent = `Downloading… ${Math.round((d.received / d.total) * 100)}%`;
  } else if (d.stage === "preparing") {
    msg.textContent = "Installing…";
  } else if (d.stage === "restarting") {
    $("#update-overlay-title").textContent = "Update complete";
    msg.textContent = "Restarting — this page reconnects by itself.";
  }
}

$("#btn-check-update").addEventListener("click", () => checkForUpdate(true));
$("#btn-update-dismiss").addEventListener("click", () => { $("#update-banner").hidden = true; });
$("#btn-update-preview").addEventListener("click", openUpdateDialog);
$("#btn-update-install").addEventListener("click", openUpdateDialog);
$("#update-close").addEventListener("click", () => closeModal("update-modal"));
$("#update-go").addEventListener("click", startUpdate);
$("#update-full").addEventListener("click", () => {
  const u = state.update || {};
  if (u.html_url) window.open(u.html_url, "_blank");
});
