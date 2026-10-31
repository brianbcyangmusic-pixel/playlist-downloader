/* Playlist Downloader — guided step-by-step tour.
 *
 * Highlights real UI elements in order (with tab switching and scrolling) and
 * explains what each one does. Runs automatically on first launch, and can be
 * replayed at any time with the "❓ How to use" button in the toolbar or by
 * calling window.startTour().
 *
 * Kept in its own IIFE so it never collides with app.js globals.
 */
(function () {
  "use strict";

  var SEEN_KEY = "pd.tour.seen.v1";

  /* ------------------------------------------------------------------ steps */
  var STEPS = [
    {
      title: "Welcome 👋",
      body:
        "This is a quick guided tour: from a playlist link to finished files. " +
        "Use <b>Next</b> / <b>Back</b> to move around, or press <code>Esc</code> to leave at " +
        "any time. You can replay it later with <b>How to use</b>."
    },
    {
      el: "#card-quick",
      title: "Quick download",
      body:
        "In a hurry? Paste any link here and press <b>Download</b> — it imports and starts " +
        "downloading in one step, using the folder and format you saved in <b>Settings</b>. " +
        "Use the panel below when you want to pick and choose tracks first."
    },
    {
      el: "#card-add",
      title: "1 · Add a playlist",
      body:
        "Paste a <b>YouTube</b> or <b>YouTube Music</b> playlist link, a <b>Spotify</b> playlist " +
        "link, a path to a <b>text / CSV file</b> of songs, or a folder path. " +
        "Leave <b>Source</b> on <i>Auto-detect</i> — the app works it out by itself."
    },
    {
      el: "#btn-import",
      title: "2 · Import it",
      body:
        "Pick <b>Download as → Music (audio)</b> for songs with tags and cover art, or " +
        "<b>Video</b> for the full video file. Then press <b>Import playlist</b>. " +
        "Importing only <i>reads</i> the list and resolves each track — nothing downloads yet."
    },
    {
      el: "#playlist-list",
      title: "3 · Your playlists",
      body:
        "Every playlist you import is saved here and stays between sessions. " +
        "Click one to load its tracks into the queue on the right."
    },
    {
      el: "#card-queue",
      title: "4 · The queue",
      body:
        "One row per track, with its status. Use the search box or the status dropdown to " +
        "narrow a big list down, and the <b>⋯</b> button on a row to retry or remove just " +
        "that track."
    },
    {
      el: ".controls",
      title: "5 · Start, pause, stop",
      body:
        "Press <b>▶ Start</b> to begin downloading — it first asks <b>where to save</b> and " +
        "remembers your choice. While a run is going you also get <b>Pause</b>, " +
        "<b>Resume</b> and <b>Stop</b>. <b>↻ Retry failed</b> re-queues everything that errored — " +
        "handy after a dropped connection."
    },
    {
      el: "#stats",
      title: "6 · Progress at a glance",
      body:
        "Live counters: total, done, skipped, downloading, pending and failed. " +
        "Close the window mid-download and the queue is saved — reopen it and press Start " +
        "to carry on where it left off."
    },
    {
      tab: "compare",
      el: "#card-compare",
      title: "7 · Already own some of these songs?",
      body:
        "The <b>Compare folder</b> tab checks a folder of music you already have against a " +
        "playlist. It reports which tracks are <b>missing</b>, which already match, and which " +
        "files are extra. Missing tracks can be pushed straight into the download queue."
    },
    {
      tab: "settings",
      el: "#card-music",
      title: "8 · Format, quality & tags",
      body:
        "Choose the output format (<code>mp3</code>, <code>m4a</code>, <code>flac</code>…), the " +
        "bitrate, and whether to write tags and embed cover art. The defaults already give you " +
        "tagged files at 320&nbsp;kbps, so you can leave this alone."
    },
    {
      tab: "settings",
      el: "#card-where",
      title: "9 · Where your files go",
      body:
        "Finished tracks land in this folder, and <b>Open folder</b> jumps straight to it in " +
        "Finder. A playlist of many songs gets its own sub-folder named after the playlist; " +
        "a single song or video is written under its own name."
    },
    {
      tab: "settings",
      el: "#card-speed",
      title: "10 · Big playlists (1,000+ songs)",
      body:
        "3–4 parallel downloads is the sweet spot. For a very large playlist, turn on " +
        "<b>Test mode</b> first: it resolves every track without downloading anything, so you " +
        "can confirm the whole list is correct before committing."
    },
    {
      tab: "settings",
      el: "#btn-save",
      title: "11 · Don't forget to save",
      body:
        "Changes on this tab only take effect after you press <b>Save settings</b>."
    },
    {
      tab: "log",
      el: "#card-log",
      title: "12 · Something looks stuck?",
      body:
        "The <b>Activity</b> tab shows a running log of what the app is doing. If a track " +
        "keeps failing, check the troubleshooting notes in the README."
    },
    {
      title: "You're all set 🎉",
      body:
        "That's the whole app: <b>add a playlist → review the queue → Start</b>. " +
        "Use <b>❓ How to use</b> to replay this tour, and <b>⏻ Quit</b> (or <code>⌘Q</code>) " +
        "to shut everything down cleanly."
    }
  ];

  /* ------------------------------------------------------------- utilities */
  var ring = null, card = null, backdrop = null;
  var active = false, index = 0, restoreFocus = null;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function q(sel) {
    return sel ? document.querySelector(sel) : null;
  }

  function isVisible(node) {
    if (!node) return false;
    var r = node.getBoundingClientRect();
    return (r.width > 0 || r.height > 0) && node.offsetParent !== null;
  }

  /* ------------------------------------------------------------- assembly */
  function build() {
    if (backdrop) return;
    backdrop = document.createElement("div");
    backdrop.className = "tour-backdrop";
    backdrop.addEventListener("click", function (e) {
      e.stopPropagation();
      e.preventDefault();
    }, true);

    ring = document.createElement("div");
    ring.className = "tour-ring";
    ring.hidden = true;

    card = document.createElement("div");
    card.className = "tour-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-label", "Guided tour");
    card.addEventListener("click", onCardClick);

    document.body.appendChild(backdrop);
    document.body.appendChild(ring);
    document.body.appendChild(card);
  }

  function onCardClick(e) {
    var btn = e.target.closest("[data-tour]");
    if (!btn) return;
    var act = btn.getAttribute("data-tour");
    if (act === "next") go(index + 1);
    else if (act === "prev") go(index - 1);
    else if (act === "end") finish();
  }

  function go(n) {
    if (n < 0) n = 0;
    if (n >= STEPS.length) { finish(); return; }
    index = n;
    render();
  }

  function render() {
    var step = STEPS[index];
    if (step.tab) {
      var tab = document.querySelector('.tab[data-tab="' + step.tab + '"]');
      if (tab && !tab.classList.contains("active")) tab.click();
    }
    // let the panel become visible, then measure
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { paint(step); });
    });
  }

  function paint(step) {
    var target = q(step.el);
    var hasTarget = isVisible(target);

    card.className = "tour-card" + (hasTarget ? "" : " center");
    card.innerHTML =
      '<div class="tour-head"><h4>' + esc(step.title) + "</h4>" +
        '<span class="tour-count">' + (index + 1) + " / " + STEPS.length + "</span></div>" +
      "<p>" + step.body + "</p>" +
      '<div class="tour-actions">' +
        '<span class="tour-dots">' +
          STEPS.map(function (_, k) {
            return '<span class="tour-dot' + (k === index ? " on" : "") + '"></span>';
          }).join("") +
        "</span>" +
        '<span class="tour-right">' +
          (index > 0 ? '<button class="ghost" data-tour="prev">‹ Back</button>' : "") +
          '<button class="ghost" data-tour="end">Skip</button>' +
          '<button class="primary" data-tour="next">' +
            (index === STEPS.length - 1 ? "Done" : "Next ›") +
          "</button>" +
        "</span>" +
      "</div>";

    if (!hasTarget) {
      card.style.left = "";
      card.style.top = "";
      ring.hidden = true;
      focusNext();
      return;
    }

    try { target.scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) {}
    requestAnimationFrame(function () { place(target); });
  }

  function place(target) {
    var pad = 8, margin = 12;
    var r = target.getBoundingClientRect();

    if (!r.width && !r.height) {
      card.className = "tour-card center";
      card.style.left = "";
      card.style.top = "";
      ring.hidden = true;
      return;
    }

    ring.hidden = false;
    ring.style.left = (r.left - pad) + "px";
    ring.style.top = (r.top - pad) + "px";
    ring.style.width = (r.width + pad * 2) + "px";
    ring.style.height = (r.height + pad * 2) + "px";

    var cw = card.offsetWidth, ch = card.offsetHeight;
    var below = window.innerHeight - r.bottom;
    var top = below > ch + 28 ? r.bottom + 14 : Math.max(margin, r.top - ch - 14);
    var left = Math.min(
      Math.max(margin, r.left + r.width / 2 - cw / 2),
      Math.max(margin, window.innerWidth - cw - margin)
    );

    card.style.left = left + "px";
    card.style.top = top + "px";
  }

  function focusNext() {
    var b = card.querySelector('[data-tour="next"]');
    if (b) b.focus();
  }

  /* ---------------------------------------------------------------- public */
  function start(from) {
    build();
    active = true;
    index = (typeof from === "number" && from >= 0 && from < STEPS.length) ? from : 0;
    restoreFocus = document.activeElement;
    backdrop.hidden = false;
    card.hidden = false;
    render();
  }

  function finish() {
    active = false;
    try { localStorage.setItem(SEEN_KEY, "1"); } catch (e) {}
    if (backdrop) backdrop.hidden = true;
    if (ring) ring.hidden = true;
    if (card) card.hidden = true;
    if (restoreFocus && restoreFocus.focus) { try { restoreFocus.focus(); } catch (e) {} }
  }

  function onKey(e) {
    if (!active) return;
    if (e.key === "Escape") { e.preventDefault(); finish(); }
    else if (e.key === "ArrowRight" || e.key === "Enter") { e.preventDefault(); go(index + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); go(index - 1); }
  }

  function reposition() {
    if (!active) return;
    var step = STEPS[index];
    var target = q(step.el);
    if (isVisible(target)) place(target);
  }

  function onDocClick(e) {
    var btn = e.target.closest && e.target.closest("#btn-help");
    if (btn) { e.preventDefault(); start(); }
  }

  /* -------------------------------------------------------------- wire up */
  build();
  backdrop.hidden = true;
  card.hidden = true;

  window.startTour = start;
  window.addEventListener("keydown", onKey);
  window.addEventListener("resize", reposition);
  window.addEventListener("scroll", reposition, true);
  document.addEventListener("click", onDocClick, true);

  var seen = null;
  try { seen = localStorage.getItem(SEEN_KEY); } catch (e) {}

  // Never start behind the sign-in gate: the tour points at elements that are
  // not on screen yet, and it would mark itself as seen without being read.
  function lockedNow() {
    return document.body.classList.contains("locked") ||
      (document.getElementById("auth-gate") && !document.getElementById("auth-gate").hidden);
  }

  // Optional deep link: open the UI with ?tour=<step> to jump straight to a step.
  var deep = /[?&]tour=(\d+)/.exec(window.location.search);
  if (deep) {
    var from = parseInt(deep[1], 10);
    var jump = function () { setTimeout(function () { start(from); }, 350); };
    if (document.readyState === "complete") jump();
    else window.addEventListener("load", jump);
  } else if (!seen) {
    var maybeStart = function () {
      setTimeout(function () {
        if (lockedNow()) return;  // stays "unseen"; runs once they sign in
        start();
      }, 700);
    };
    if (document.readyState === "complete") maybeStart();
    else window.addEventListener("load", maybeStart);

    // If the gate was up, the tour skipped itself; run it after signing in.
    if (window.authReady) {
      window.authReady.then(function () {
        setTimeout(function () {
          var now = null;
          try { now = localStorage.getItem(SEEN_KEY); } catch (e) {}
          if (now || lockedNow()) return;
          start();
        }, 900);
      });
    }
  }
})();
