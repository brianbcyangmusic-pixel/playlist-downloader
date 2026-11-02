/* Playlist Downloader — admin dashboard.
 *
 * Reads the `downloads` and `errors` collections that the desktop app writes
 * (see app/telemetry.py). The Firestore rules only let the owner email read
 * them, so signing in as anyone else shows an "access denied" gate rather
 * than an empty dashboard.
 *
 * There is no build step: this file is loaded as-is by admin/index.html.
 */
(function () {
  "use strict";

  var CONFIG = {
    apiKey: "AIzaSyCSCx1rMiR2bQKFH6PXwGRLUxg0__pkmdo",
    authDomain: "playlist-downloader-99ad0.firebaseapp.com",
    projectId: "playlist-downloader-99ad0",
    storageBucket: "playlist-downloader-99ad0.firebasestorage.app",
    messagingSenderId: "800139508246",
    appId: "1:800139508246:web:5032688053764cd0871337"
  };

  var ADMIN_EMAIL = "brian.bcyang27@gmail.com";
  var DOWNLOAD_LIMIT = 1000;
  var ERROR_LIMIT = 500;

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var state = {
    downloads: [],
    errors: [],
    tab: "overview",
    expanded: null,
    user: null,
    timer: null
  };

  /* ------------------------------------------------------------ formatting */
  function asDate(value) {
    if (!value) return null;
    var d = new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }

  function fmtWhen(value) {
    var d = asDate(value);
    if (!d) return "—";
    var now = Date.now();
    var diff = Math.round((now - d.getTime()) / 1000);
    if (diff >= 0 && diff < 60) return "just now";
    if (diff >= 0 && diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff >= 0 && diff < 86400) return Math.floor(diff / 3600) + "h ago";
    if (diff >= 0 && diff < 604800) return Math.floor(diff / 86400) + "d ago";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function fmtFull(value) {
    var d = asDate(value);
    return d ? d.toLocaleString() : "—";
  }

  function fmtDuration(seconds) {
    var s = Number(seconds);
    if (!s || s < 0) return "—";
    if (s < 60) return Math.round(s) + "s";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m " + Math.round(s % 60) + "s";
    var h = Math.floor(m / 60);
    return h + "h " + (m % 60) + "m";
  }

  function statusBadge(status) {
    var s = String(status || "unknown").toLowerCase();
    if (["done", "partial", "failed", "started"].indexOf(s) === -1) s = "interrupted";
    var label = { done: "Done", partial: "Partial", failed: "Failed", started: "Running", interrupted: "Unknown" }[s];
    return '<span class="badge ' + s + '">' + label + "</span>";
  }

  function tracks(doc) {
    var total = Number(doc.tracks_total || 0);
    var done = Number(doc.tracks_done || 0);
    return done + "/" + total;
  }

  /* ---------------------------------------------------------------- login */
  firebase.initializeApp(CONFIG);
  var auth = firebase.auth();
  var db = firebase.firestore();

  var gate = $("#gate");
  var app = $("#app");
  var loginForm = $("#login-form");
  var loginMsg = $("#login-msg");
  var loginButton = $("#login-submit");

  function showLogin(message, ok) {
    gate.hidden = false;
    app.hidden = true;
    loginMsg.textContent = message || "";
    loginMsg.className = "msg" + (ok ? " ok" : "");
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  function friendlyAuthError(err) {
    var code = (err && err.code) || "";
    var map = {
      "auth/invalid-email": "That is not a valid email address.",
      "auth/user-not-found": "No account uses that email.",
      "auth/wrong-password": "That password is not right.",
      "auth/invalid-credential": "That email and password do not match.",
      "auth/invalid-login-credentials": "That email and password do not match.",
      "auth/too-many-requests": "Too many attempts. Wait a minute and try again.",
      "auth/user-disabled": "That account has been disabled.",
      "auth/network-request-failed": "Could not reach the network. Check your connection.",
      "auth/operation-not-allowed": "Email sign-in is turned off for this project."
    };
    return map[code] || (err && err.message) || "Sign-in failed.";
  }

  loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var email = $("#login-email").value.trim();
    var password = $("#login-password").value;
    if (!email || !password) { showLogin("Enter an email and a password."); return; }
    loginButton.disabled = true;
    loginButton.textContent = "Signing in…";
    loginMsg.textContent = "";
    auth.signInWithEmailAndPassword(email, password).then(function () {
      // onAuthStateChanged takes over from here.
    }).catch(function (err) {
      showLogin(friendlyAuthError(err));
    }).then(function () {
      loginButton.disabled = false;
      loginButton.textContent = "Sign in";
    });
  });

  auth.onAuthStateChanged(function (user) {
    state.user = user;
    if (!user) {
      showLogin("");
      return;
    }
    if ((user.email || "").toLowerCase() !== ADMIN_EMAIL.toLowerCase()) {
      auth.signOut();
      showLogin("Access denied. Only " + ADMIN_EMAIL + " can open this dashboard.");
      return;
    }
    gate.hidden = true;
    app.hidden = false;
    $("#who").textContent = user.email;
    $("#login-password").value = "";
    load(true);
  });

  $("#signout").addEventListener("click", function () {
    auth.signOut();
  });

  /* ------------------------------------------------------------- data load */
  function load(showLoading) {
    if (!state.user) return;
    if (showLoading) {
      $("#recent-downloads").innerHTML = '<tr><td colspan="7" class="loading">Loading…</td></tr>';
      $("#downloads-body").innerHTML = '<tr><td colspan="9" class="loading">Loading…</td></tr>';
      $("#errors-body").innerHTML = '<tr><td colspan="6" class="loading">Loading…</td></tr>';
      $("#recent-errors").innerHTML = '<tr><td colspan="5" class="loading">Loading…</td></tr>';
    }
    $("#data-error").hidden = true;

    Promise.all([
      db.collection("downloads").orderBy("created_at", "desc").limit(DOWNLOAD_LIMIT).get(),
      db.collection("errors").orderBy("created_at", "desc").limit(ERROR_LIMIT).get()
    ]).then(function (results) {
      state.downloads = results[0].docs.map(function (doc) {
        return Object.assign({ _id: doc.id }, doc.data());
      });
      state.errors = results[1].docs.map(function (doc) {
        return Object.assign({ _id: doc.id }, doc.data());
      });
      renderAll();
      var stamp = new Date().toLocaleTimeString();
      $("#updated").innerHTML = '<span class="live"></span>updated ' + esc(stamp);
    }).catch(function (err) {
      var message = (err && err.code === "permission-denied")
        ? "Firestore refused the read. The signed-in account is not the owner in the security rules."
        : (err && err.message) || "Could not load data.";
      $("#data-error").textContent = message;
      $("#data-error").hidden = false;
      $("#updated").innerHTML = '<span class="live stale"></span>not updated';
    });
  }

  $("#refresh").addEventListener("click", function () { load(true); });

  // Light auto-refresh while the tab is visible.
  setInterval(function () {
    if (!state.user || document.hidden) return;
    load(false);
  }, 60000);

  /* ------------------------------------------------------------- rendering */
  function renderAll() {
    renderOverview();
    renderDownloads();
    renderErrors();
    renderUsers();
    $("#tab-downloads").textContent = state.downloads.length;
    $("#tab-errors").textContent = state.errors.length;
    $("#tab-users").textContent = Object.keys(groupUsers()).length;
  }

  function renderOverview() {
    var d = state.downloads;
    var done = d.filter(function (r) { return r.status === "done"; }).length;
    var partial = d.filter(function (r) { return r.status === "partial"; }).length;
    var failed = d.filter(function (r) { return r.status === "failed"; }).length;
    var downloaded = d.reduce(function (sum, r) { return sum + Number(r.tracks_done || 0); }, 0);
    var unread = state.errors.filter(function (r) { return r.notified !== true; }).length;

    $("#s-downloads").textContent = d.length;
    $("#s-done").textContent = done;
    $("#s-partial").textContent = partial;
    $("#s-failed").textContent = failed;
    $("#s-tracks").textContent = downloaded;
    $("#s-users").textContent = Object.keys(groupUsers()).length;
    $("#s-errors").textContent = unread;

    var recent = d.slice(0, 8);
    $("#recent-downloads").innerHTML = recent.length
      ? recent.map(function (r) {
          return "<tr>" +
            '<td class="num" title="' + esc(fmtFull(r.created_at)) + '">' + esc(fmtWhen(r.created_at)) + "</td>" +
            "<td>" + esc(r.user_email || "unknown") + "</td>" +
            '<td class="ellipsis" title="' + esc(r.playlist || "") + '">' + esc(r.playlist || "—") + "</td>" +
            '<td class="hide-sm">' + esc(platformLabel(r.platform)) + "</td>" +
            '<td class="num">' + esc(tracks(r)) + "</td>" +
            "<td>" + statusBadge(r.status) + "</td>" +
            '<td class="hide-sm">' + esc(r.os || "—") + "</td>" +
          "</tr>";
        }).join("")
      : '<tr><td colspan="7" class="empty">No downloads recorded yet.</td></tr>';

    var errs = state.errors.slice(0, 5);
    $("#recent-errors").innerHTML = errs.length
      ? errs.map(function (e) {
          return "<tr>" +
            '<td class="num" title="' + esc(fmtFull(e.created_at)) + '">' + esc(fmtWhen(e.created_at)) + "</td>" +
            "<td>" + esc(e.user_email || e.email || "unknown") + "</td>" +
            '<td class="hide-sm">' + esc(e.kind || "app_error") + "</td>" +
            '<td class="wrap">' + esc(e.message || "") + "</td>" +
            '<td class="actions">' + (e.notified === true ? '<span class="muted">reviewed</span>'
              : '<button class="er-mark" data-id="' + esc(e._id) + '">Mark reviewed</button>') + "</td>" +
          "</tr>";
        }).join("")
      : '<tr><td colspan="5" class="empty">No errors recorded. Good news.</td></tr>';
  }

  function platformLabel(value) {
    var v = String(value || "").toLowerCase();
    if (!v) return "—";
    if (v === "youtube") return "YouTube";
    if (v === "youtube music" || v === "ytmusic") return "YouTube Music";
    if (v === "spotify") return "Spotify";
    if (v === "text") return "Text list";
    if (v === "folder") return "Folder";
    return value;
  }

  function matches(record, needle, fields) {
    if (!needle) return true;
    var hay = fields.map(function (f) { return String(record[f] || ""); }).join(" ").toLowerCase();
    return hay.indexOf(needle) !== -1;
  }

  function filteredDownloads() {
    var needle = $("#dl-search").value.trim().toLowerCase();
    var status = $("#dl-status").value;
    var platform = $("#dl-platform").value;
    return state.downloads.filter(function (r) {
      if (status && String(r.status || "") !== status) return false;
      if (platform && String(r.platform || "").toLowerCase() !== platform) return false;
      return matches(r, needle, ["playlist", "user_email", "link", "os"]);
    });
  }

  function renderDownloads() {
    var rows = filteredDownloads();
    var body = $("#downloads-body");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="9" class="empty">No downloads match.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (r) {
      var open = state.expanded === r._id;
      var main = "<tr>" +
        '<td class="num" title="' + esc(fmtFull(r.created_at)) + '">' + esc(fmtWhen(r.created_at)) + "</td>" +
        "<td>" + esc(r.user_email || "unknown") + "</td>" +
        '<td class="ellipsis" title="' + esc(r.playlist || "") + '">' + esc(r.playlist || "—") + "</td>" +
        '<td class="hide-sm">' + esc(platformLabel(r.platform)) + "</td>" +
        '<td class="num">' + esc(r.tracks_done || 0) + "</td>" +
        '<td class="num">' + esc(r.tracks_failed || 0) + "</td>" +
        "<td>" + statusBadge(r.status) + "</td>" +
        '<td class="hide-sm num">' + esc(fmtDuration(r.duration_s)) + "</td>" +
        '<td class="actions"><button class="dl-toggle" data-id="' + esc(r._id) + '">' +
          (open ? "Hide" : "Details") + "</button></td>" +
      "</tr>";
      if (!open) return main;

      var samples = Array.isArray(r.sample_tracks) ? r.sample_tracks : [];
      var details = '<tr class="detail"><td colspan="9"><dl>' +
        "<dt>Playlist</dt><dd>" + esc(r.playlist || "—") + "</dd>" +
        "<dt>Link</dt><dd>" + (r.link
          ? '<a href="' + esc(r.link) + '" target="_blank" rel="noreferrer noopener">' + esc(r.link) + "</a>"
          : "—") + "</dd>" +
        "<dt>User</dt><dd>" + esc(r.user_email || "unknown") + " · <code>" + esc(r.uid || "no uid") + "</code></dd>" +
        "<dt>Tracks</dt><dd>" + esc(r.tracks_done || 0) + " done · " + esc(r.tracks_failed || 0) + " failed · " +
          esc(r.tracks_skipped || 0) + " skipped · " + esc(r.tracks_total || 0) + " total</dd>" +
        "<dt>Status</dt><dd>" + esc(r.status || "unknown") + "</dd>" +
        "<dt>Started</dt><dd>" + esc(fmtFull(r.started_at)) + "</dd>" +
        "<dt>Finished</dt><dd>" + esc(fmtFull(r.finished_at)) + "</dd>" +
        "<dt>Duration</dt><dd>" + esc(fmtDuration(r.duration_s)) + "</dd>" +
        "<dt>Machine</dt><dd>" + esc(r.os || "—") + " · app " + esc(r.app_version || "—") + "</dd>" +
        "<dt>Document</dt><dd><code>" + esc(r._id) + "</code></dd>" +
      "</dl>" +
      (samples.length ? '<ul class="samples">' + samples.map(function (t) {
        return "<li>" + esc(t) + "</li>";
      }).join("") + "</ul>" : "") +
      "</td></tr>";
      return main + details;
    }).join("");
  }

  function filteredErrors() {
    var needle = $("#er-search").value.trim().toLowerCase();
    var mode = $("#er-state").value;
    return state.errors.filter(function (e) {
      if (mode === "unread" && e.notified === true) return false;
      if (mode === "read" && e.notified !== true) return false;
      return matches(e, needle, ["message", "user_email", "email", "kind"]);
    });
  }

  function renderErrors() {
    var rows = filteredErrors();
    var body = $("#errors-body");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No errors match.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (e) {
      return "<tr>" +
        '<td class="num" title="' + esc(fmtFull(e.created_at)) + '">' + esc(fmtWhen(e.created_at)) + "</td>" +
        "<td>" + esc(e.user_email || e.email || "unknown") + "</td>" +
        '<td class="hide-sm">' + esc(e.kind || "app_error") + "</td>" +
        '<td class="wrap">' + esc(e.message || "") + "</td>" +
        '<td class="hide-sm">' + esc(e.app_version || "—") + "</td>" +
        '<td class="actions">' + (e.notified === true
          ? '<span class="muted">reviewed</span>'
          : '<button class="er-mark" data-id="' + esc(e._id) + '">Mark reviewed</button>') +
          "</td>" +
      "</tr>";
    }).join("");
  }

  function groupUsers() {
    var byEmail = {};
    state.downloads.forEach(function (r) {
      var key = String(r.user_email || "unknown").toLowerCase();
      if (!byEmail[key]) {
        byEmail[key] = {
          email: r.user_email || "unknown", uid: r.uid || "", runs: 0, tracks: 0,
          platforms: {}, os: {}, last: r.created_at
        };
      }
      var u = byEmail[key];
      u.runs += 1;
      u.tracks += Number(r.tracks_done || 0);
      if (r.platform) u.platforms[platformLabel(r.platform)] = true;
      if (r.os) u.os[r.os] = true;
      if (String(r.created_at || "") > String(u.last || "")) u.last = r.created_at;
    });
    return byEmail;
  }

  function renderUsers() {
    var needle = $("#user-search").value.trim().toLowerCase();
    var users = groupUsers();
    var rows = Object.keys(users).map(function (k) { return users[k]; })
      .filter(function (u) {
        return !needle || (u.email + " " + u.uid).toLowerCase().indexOf(needle) !== -1;
      })
      .sort(function (a, b) { return String(b.last).localeCompare(String(a.last)); });

    $("#users-note").textContent = Object.keys(users).length + " user(s) with activity";
    var body = $("#users-body");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty">No users match.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (u) {
      return "<tr>" +
        "<td>" + esc(u.email) + "</td>" +
        '<td><code>' + esc(u.uid || "—") + "</code></td>" +
        '<td class="num">' + esc(u.runs) + "</td>" +
        '<td class="num">' + esc(u.tracks) + "</td>" +
        '<td class="hide-sm">' + esc(Object.keys(u.platforms).join(", ") || "—") + "</td>" +
        '<td class="hide-sm">' + esc(Object.keys(u.os).join(", ") || "—") + "</td>" +
        '<td class="num" title="' + esc(fmtFull(u.last)) + '">' + esc(fmtWhen(u.last)) + "</td>" +
      "</tr>";
    }).join("");
  }

  /* ------------------------------------------------------------- actions */
  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button") : null;
    if (!button) return;

    if (button.classList.contains("dl-toggle")) {
      var id = button.getAttribute("data-id");
      state.expanded = state.expanded === id ? null : id;
      renderDownloads();
      return;
    }

    if (button.classList.contains("er-mark")) {
      markReviewed([button.getAttribute("data-id")], button);
    }
  });

  $("#er-mark-all").addEventListener("click", function () {
    var ids = filteredErrors()
      .filter(function (e) { return e.notified !== true; })
      .map(function (e) { return e._id; });
    if (!ids.length) return;
    markReviewed(ids, $("#er-mark-all"));
  });

  function markReviewed(ids, button) {
    if (!ids.length) return;
    var batch = db.batch();
    ids.forEach(function (id) {
      batch.update(db.collection("errors").doc(id), { notified: true });
    });
    var label = button.textContent;
    button.disabled = true;
    batch.commit().then(function () {
      state.errors.forEach(function (e) {
        if (ids.indexOf(e._id) !== -1) e.notified = true;
      });
      renderAll();
    }).catch(function (err) {
      $("#data-error").textContent = "Could not update: " + ((err && err.message) || "unknown error");
      $("#data-error").hidden = false;
    }).then(function () {
      button.disabled = false;
      button.textContent = label;
    });
  }

  /* ------------------------------------------------------------ csv export */
  function csvCell(value) {
    var text = String(value == null ? "" : value);
    return '"' + text.replace(/"/g, '""') + '"';
  }

  function exportCsv(name, headers, rows) {
    var lines = [headers.map(csvCell).join(",")].concat(rows.map(function (row) {
      return row.map(csvCell).join(",");
    }));
    var blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  $("#dl-export").addEventListener("click", function () {
    exportCsv("downloads-" + new Date().toISOString().slice(0, 10) + ".csv",
      ["created_at", "user_email", "uid", "playlist", "platform", "link", "tracks_total",
       "tracks_done", "tracks_failed", "tracks_skipped", "status", "os", "app_version",
       "started_at", "finished_at", "duration_s"],
      filteredDownloads().map(function (r) {
        return [r.created_at, r.user_email, r.uid, r.playlist, r.platform, r.link,
          r.tracks_total, r.tracks_done, r.tracks_failed, r.tracks_skipped, r.status,
          r.os, r.app_version, r.started_at, r.finished_at, r.duration_s];
      }));
  });

  $("#er-export").addEventListener("click", function () {
    exportCsv("errors-" + new Date().toISOString().slice(0, 10) + ".csv",
      ["created_at", "kind", "message", "user_email", "email", "uid", "app_version", "notified"],
      filteredErrors().map(function (e) {
        return [e.created_at, e.kind, e.message, e.user_email, e.email, e.uid,
          e.app_version, e.notified];
      }));
  });

  /* ----------------------------------------------------------------- tabs */
  $$(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      var name = tab.getAttribute("data-tab");
      state.tab = name;
      $$(".tab").forEach(function (t) { t.classList.toggle("active", t === tab); });
      $$(".panel").forEach(function (p) {
        p.classList.toggle("active", p.id === "panel-" + name);
      });
    });
  });

  ["#dl-search", "#dl-status", "#dl-platform"].forEach(function (sel) {
    $(sel).addEventListener("input", renderDownloads);
    $(sel).addEventListener("change", renderDownloads);
  });
  ["#er-search", "#er-state"].forEach(function (sel) {
    $(sel).addEventListener("input", renderErrors);
    $(sel).addEventListener("change", renderErrors);
  });
  $("#user-search").addEventListener("input", renderUsers);
})();
