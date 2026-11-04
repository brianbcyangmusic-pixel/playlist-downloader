/* One-time setup card: installs ffmpeg / JS runtime with live progress. */

const SPEED_SAMPLE_MS = 250;
const setupProgress = { file: 0, received: 0, at: 0, speed: 0 };

function resetSpeed(file = 0) {
  Object.assign(setupProgress, { file, received: 0, at: 0, speed: 0 });
}

function showSetupProgress({ fraction = 0, text, button }) {
  // Ease-out: early progress moves quickly and the final stretch slows down,
  // while still ending at exactly 100%.
  const pct = Math.round((1 - Math.pow(1 - Math.min(1, fraction), 2)) * 100);
  $("#tools-progress").hidden = false;
  $("#tools-progress-fill").style.width = `${pct}%`;
  $("#tools-progress-pct").textContent = `${pct}%`;
  if (text !== undefined) $("#tools-progress-text").textContent = text;
  if (button !== undefined) $("#btn-tools-install").textContent = button;
}

function resetSetupProgress() {
  resetSpeed();
  $("#tools-progress").hidden = true;
  $("#tools-progress-fill").style.width = "0%";
  $("#tools-progress-pct").textContent = "0%";
  $("#tools-progress-text").textContent = "Connecting…";
}

function renderToolsSetup(toolsInfo) {
  const card = $("#card-tools");
  if (!toolsInfo) return;
  const setup = toolsInfo.setup || {};
  const missing = setup.needed || [];
  if (!missing.length) { card.hidden = true; return; }

  card.hidden = false;
  const btn = $("#btn-tools-install");
  if (setup.auto_install) {
    $("#tools-hint").innerHTML =
      `This computer needs <b>${esc(missing.join(" and "))}</b> before audio can be ` +
      `extracted. One click installs them into the app's own folder (about 120 MB, ` +
      `once — they stay put after updates).`;
    if (!btn.disabled) resetSetupProgress();
    btn.hidden = false;
    btn.textContent = "Install required tools";
  } else {
    $("#tools-hint").innerHTML =
      `This computer is missing <b>${esc(missing.join(" and "))}</b>. Install them, then restart the app:`;
    btn.hidden = true;
    $("#tools-status").innerHTML = "<code>brew install ffmpeg deno</code>";
    resetSetupProgress();
  }
}

/** Smoothed download speed from successive byte counts. */
function sampleSpeed(received) {
  const now = performance.now();
  if (!setupProgress.at) {
    Object.assign(setupProgress, { received, at: now });
  } else if (now - setupProgress.at >= SPEED_SAMPLE_MS) {
    const instant = Math.max(0, received - setupProgress.received) / ((now - setupProgress.at) / 1000);
    setupProgress.speed = setupProgress.speed ? setupProgress.speed * 0.7 + instant * 0.3 : instant;
    Object.assign(setupProgress, { received, at: now });
  }
  return setupProgress.speed;
}

function onToolsProgress(d) {
  const btn = $("#btn-tools-install");
  const files = Math.max(1, d.files || 1);
  const file = Math.max(1, d.file || 1);
  const tool = d.tool || "tools";

  switch (d.stage) {
    case "starting":
      btn.disabled = true;
      resetSpeed(file);
      showSetupProgress({ fraction: (file - 1) / files, text: `Connecting to the ${tool} download…`, button: `Preparing ${tool}…` });
      break;
    case "downloading": {
      btn.disabled = true;
      if (setupProgress.file !== file) resetSpeed(file);
      // Bytes across all files, so the bar covers the whole setup.
      const part = d.total ? Math.min(1, d.received / d.total) : 0;
      const speed = sampleSpeed(d.received);
      const amount = d.total ? `${fmtBytes(d.received)} of ${fmtBytes(d.total)}` : fmtBytes(d.received);
      const rate = speed > 0 ? ` · ${fmtBytes(speed)}/s` : "";
      const which = files > 1 ? ` (${file} of ${files})` : "";
      showSetupProgress({ fraction: (file - 1 + part) / files, text: `${tool}${which} — ${amount}${rate}`, button: `Installing ${tool}…` });
      break;
    }
    case "unpacking":
      btn.disabled = true;
      showSetupProgress({ fraction: file / files, text: `Unpacking ${tool}…`, button: `Unpacking ${tool}…` });
      break;
    case "done":
      showSetupProgress({ fraction: 1, text: "Finished — the tools are ready." });
      toast("Setup finished", d.message || "Everything is ready.", "ok");
      setTimeout(() => { $("#card-tools").hidden = true; resetSetupProgress(); loadState(); }, 900);
      break;
    case "error":
      resetSetupProgress();
      btn.disabled = false;
      btn.textContent = "Try again";
      $("#tools-status").textContent = "";
      toast("Setup failed", d.error || "Check your internet connection and try again.", "error");
      break;
  }
}

$("#btn-tools-install").addEventListener("click", async () => {
  const btn = $("#btn-tools-install");
  btn.disabled = true;
  resetSetupProgress();
  showSetupProgress({ fraction: 0, text: "Connecting…", button: "Starting…" });
  try {
    await api("/api/tools/install", { method: "POST", body: {} });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Install required tools";
    resetSetupProgress();
    toast("Could not start setup", e.message, "error");
  }
});
