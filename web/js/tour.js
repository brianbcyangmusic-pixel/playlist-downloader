/* Playlist Downloader — guided step-by-step tour.
 *
 * Highlights real UI elements in order (with tab switching and scrolling) and
 * explains what each one does. Runs automatically on first launch, and can be
 * replayed at any time with the "Guide" button in the sidebar or by
 * calling window.startTour().
 *
 * Kept in its own IIFE so it never collides with the shared globals in js/.
 */
(function () {
  "use strict";

  var SEEN_KEY = "pd.tour.seen.v1";

  /* ------------------------------------------------------------------ steps */
  var STEPS = [
    {
      title: "Welcome",
      body:
        "A quick tour: from a playlist link to finished files. " +
        "Use <b>Next</b> / <b>Back</b> or the arrow keys, and <code>Esc</code> to leave at " +
        "any time. Replay it later with <b>Guide</b> in the sidebar."
    },
    {
      el: "#drop-form",
      title: "1 · Paste a link",
      body:
        "A <b>YouTube</b>, <b>YouTube Music</b> or <b>Spotify</b> playlist, a single video, " +
        "a path to a <b>text / CSV file</b> of songs, or a folder path. You can also drag a link " +
        "straight onto this box. Press <kbd>/</kbd> from anywhere to jump here."
    },
    {
      el: ".hero-row",
      title: "2 · Choose how",
      body:
        "Pick <b>Music</b> for tagged audio with cover art, or <b>Video</b> for the full file. " +
        "<b>Download now</b> imports and starts in one step. <b>Add to library</b> only reads the " +
        "list, so you can review the tracks before downloading."
    },
    {
      el: "#run-deck",
      title: "3 · Progress & controls",
      body:
        "The bar shows everything at a glance: done, skipped, downloading and failed. " +
        "<b>Start</b> asks where to save and remembers your choice; while running you get " +
        "<b>Pause</b>, <b>Resume</b> and <b>Stop</b>. <b>Retry failed</b> re-queues anything that errored."
    },
    {
      el: "#card-library",
      title: "4 · Your library",
      body:
        "Every playlist you import is kept here between sessions, with a ring showing how much " +
        "of it is finished. Click one to load its tracks."
    },
    {
      el: "#card-queue",
      title: "5 · The queue",
      body:
        "One row per track. Filter by name, or by status with the chips. Hover a row to " +
        "<b>retry</b>, <b>skip</b>, or <b>show the file</b> in its folder."
    },
    {
      tab: "compare",
      el: "#card-compare",
      title: "6 · Already own some of these?",
      body:
        "<b>Compare</b> checks a folder of music against a playlist and reports what's " +
        "<b>missing</b>, what matches, and what's extra. Missing tracks can be queued in one click."
    },
    {
      tab: "settings",
      el: "#card-where",
      title: "7 · Where files go",
      body:
        "Finished tracks land in this folder. A playlist of many songs gets its own sub-folder " +
        "named after the playlist."
    },
    {
      tab: "settings",
      el: "#card-music",
      title: "8 · Format & quality",
      body:
        "The defaults give you tagged <code>mp3</code> files at 320&nbsp;kbps, so you can " +
        "leave this alone. Changes show a <b>Save settings</b> bar at the bottom " +
        "(<kbd>⌘S</kbd> works too)."
    },
    {
      tab: "log",
      el: "#card-log",
      title: "9 · Something looks stuck?",
      body:
        "<b>Activity</b> is a running log of what the app is doing. A red dot appears in the " +
        "sidebar when something fails."
    },
    {
      title: "You're all set",
      body:
        "That's the whole app: <b>paste a link → Download now</b>. " +
        "Use <b>Guide</b> to replay this tour, and <b>Quit</b> to shut everything down cleanly."
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
    var tab = document.querySelector('.tab[data-tab="' + (step.tab || "download") + '"]');
    if (tab && !tab.classList.contains("active")) tab.click();
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
