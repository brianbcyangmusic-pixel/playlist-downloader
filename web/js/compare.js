/* Compare tab: check a folder against a playlist and act on what's missing. */

const COMPARE_POLL_MS = 600;
let comparePoll = null;

function renderComparePlaylists() {
  const sel = $("#compare-playlist");
  const cur = sel.value;
  sel.innerHTML = `<option value="">Choose a playlist…</option>` + state.playlists.map(p =>
    `<option value="${p.id}">${esc(p.name)} (${p.total})</option>`).join("");
  if (cur) sel.value = cur;
}

$("#compare-threshold").addEventListener("input", e => {
  const v = Number(e.target.value);
  $("#threshold-label").textContent = v <= 0.6 ? "loose" : v >= 0.85 ? "strict" : "balanced";
});

$("#compare-ignore-extra").addEventListener("change", () => {
  if (state.lastCompare) renderCompare(state.lastCompare, $("#compare-ignore-extra").checked);
});

$$(".mini-tab").forEach(tab => tab.addEventListener("click", () => {
  $$(".mini-tab").forEach(t => t.classList.toggle("active", t === tab));
  $$(".cres").forEach(c => c.classList.toggle("active", c.id === `cres-${tab.dataset.cres}`));
}));

$("#btn-browse").addEventListener("click", () =>
  openBrowser($("#compare-folder").value || "", path => { $("#compare-folder").value = path; }));

$("#btn-compare").addEventListener("click", async () => {
  const playlistId = $("#compare-playlist").value;
  const folder = $("#compare-folder").value.trim();
  if (!playlistId) { toast("Pick a playlist", "Choose which playlist to check.", "warn"); $("#compare-playlist").focus(); return; }
  if (!folder) { toast("Pick a folder", "Choose the folder to scan.", "warn"); $("#compare-folder").focus(); return; }

  $("#btn-compare").disabled = true;
  $("#compare-status").textContent = "Starting…";
  try {
    const { job } = await api("/api/compare", {
      method: "POST",
      body: {
        playlist_id: Number(playlistId),
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
    catch (e) {
      clearInterval(comparePoll);
      $("#btn-compare").disabled = false;
      toast("Compare failed", e.message, "error");
      return;
    }
    if (job.status === "running") return;
    clearInterval(comparePoll);
    $("#btn-compare").disabled = false;
    $("#compare-status").textContent = "";
    if (job.status === "error") { toast("Compare failed", job.error, "error"); return; }
    state.lastCompare = job.result;
    renderCompare(job.result, $("#compare-ignore-extra").checked);
    const c = job.result.counts;
    toast("Comparison complete", `${c.missing} missing of ${c.playlist} tracks`, c.missing ? "warn" : "ok");
  }, COMPARE_POLL_MS);
}

function onCompareProgress(d) {
  $("#compare-status").textContent = d.message || "";
  $("#btn-compare").disabled = d.stage !== "done" && d.stage !== "error";
}

const tableEmpty = (cols, title) =>
  `<tr class="table-empty"><td colspan="${cols}">${emptyState("compare", title)}</td></tr>`;

function renderCompare(r, ignoreExtra) {
  $("#compare-results").hidden = false;
  const c = r.counts;
  $("#compare-summary").innerHTML = `
    <div class="summary-chip"><b>${c.playlist}</b>in playlist</div>
    <div class="summary-chip"><b>${c.folder}</b>files on disk</div>
    <div class="summary-chip missing"><b>${c.missing}</b>missing</div>
    <div class="summary-chip matched"><b>${c.matched}</b>matched</div>
    ${ignoreExtra ? "" : `<div class="summary-chip extra"><b>${c.extra}</b>extra</div>`}`;
  $("#compare-summary").style.gridTemplateColumns = ignoreExtra ? "repeat(4, 1fr)" : "";
  $(".mini-tab[data-cres=extra]").hidden = ignoreExtra;
  if (ignoreExtra && $(".mini-tab[data-cres=extra]").classList.contains("active")) {
    $(".mini-tab[data-cres=missing]").click();
  }
  $("#btn-import-missing").disabled = !r.missing.length;
  $("#btn-export-missing").disabled = !r.missing.length;

  $("#missing-table tbody").innerHTML = r.missing.length ? r.missing.map((m, i) => `
    <tr>
      <td class="col-num">${i + 1}</td>
      <td><div class="t-title" title="${esc(m.title)}">${esc(m.title)}</div>${m.artist ? `<div class="t-artist">${esc(m.artist)}</div>` : ""}</td>
      <td><div class="cell-file" title="${esc((m.closest || {}).file || "")}">${
        m.closest ? `${esc(m.closest.name)} <span class="muted">(${m.closest.score})</span>` : "—"
      }</div></td>
    </tr>`).join("") : tableEmpty(3, "Nothing missing — every track is on disk.");

  $("#matched-table tbody").innerHTML = r.matched.length ? r.matched.map((m, i) => `
    <tr>
      <td class="col-num">${i + 1}</td>
      <td><div class="t-title">${esc(m.title)}</div>${m.artist ? `<div class="t-artist">${esc(m.artist)}</div>` : ""}</td>
      <td><div class="cell-file" title="${esc(m.file)}">${esc(m.file_name)}</div></td>
      <td class="col-score"><span class="score ${m.score > 0.9 ? "hi" : ""}">${m.score}</span></td>
    </tr>`).join("") : tableEmpty(4, "No matches.");

  $("#extra-table tbody").innerHTML = r.extra.length ? r.extra.map((m, i) => `
    <tr>
      <td class="col-num">${i + 1}</td>
      <td><div class="cell-file" title="${esc(m.file)}">${esc(m.name)}</div></td>
      <td>${esc(m.artist || "")}</td>
      <td>${esc(m.album || "")}</td>
    </tr>`).join("") : tableEmpty(4, "No extra files.");
}

$("#btn-export-missing").addEventListener("click", () => {
  const r = state.lastCompare;
  if (!r || !r.missing.length) { toast("Nothing to export", "", "warn"); return; }
  const lines = r.missing.map(m => (m.artist ? `${m.artist} - ${m.title}` : m.title));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/plain" }));
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
          title: m.title, artist: m.artist, album: m.album, duration: m.duration, url: m.url,
        })),
      },
    });
    toast("Added to library", `${res.added} tracks in a new playlist. Press Start to download.`, "ok");
    await loadPlaylists();
    await selectPlaylist(res.playlist_id);
    showTab("download");
  } catch (e) { toast("Could not add", e.message, "error"); }
});
