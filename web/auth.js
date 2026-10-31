/* Sign-in gate.
 *
 * Runs before app.js. If this build has accounts configured, nothing else is
 * usable until someone signs in; otherwise it resolves immediately and the app
 * behaves exactly as it always has.
 *
 * Exposes `window.authReady` (a promise app.js awaits) and `window.Auth` for
 * the account chip / sign-out.
 */
(function () {
  "use strict";

  let resolveReady;
  window.authReady = new Promise((r) => { resolveReady = r; });

  const $ = (sel) => document.querySelector(sel);
  const gate = $("#auth-gate");
  const form = $("#auth-form");
  const errorBox = $("#auth-error");
  const emailEl = $("#auth-email");
  const passEl = $("#auth-password");
  const nameEl = $("#auth-name");
  const nameWrap = $("#auth-name-wrap");
  const submit = $("#auth-submit");
  const toggle = $("#auth-toggle");
  const subtitle = $("#auth-sub");
  const title = $("#auth-title");

  let mode = "signin";        // signin | signup
  let signedIn = false;
  let pollTimer = null;

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showError(message) {
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }

  function busy(on, label) {
    submit.disabled = on;
    const g = $("#btn-google");
    if (g) g.disabled = on;
    submit.textContent = on ? (label || "Working…") : (mode === "signup" ? "Create account" : "Sign in");
  }

  function unlock(auth) {
    signedIn = true;
    gate.hidden = true;
    document.body.classList.remove("locked");
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

    const chip = $("#account");
    const email = (auth && auth.user && auth.user.email) || "";
    if (chip && email) {
      chip.hidden = false;
      $("#account-email").textContent = email;
      const label = auth.user && auth.user.name ? auth.user.name : email;
      $("#btn-account").title = `${label} — signed in`;
    }
    resolveReady();
  }

  function lock(auth) {
    signedIn = false;
    gate.hidden = false;
    document.body.classList.add("locked");
    if (auth && auth.configured === false) {
      title.textContent = "Accounts are not set up";
      subtitle.textContent =
        "This copy of the app has no account backend configured, so sign-in is unavailable.";
      form.hidden = true;
      return;
    }
    const google = $("#btn-google");
    const or = $("#auth-or");
    if (auth && auth.google) {
      google.hidden = false;
      or.hidden = false;
    } else {
      google.hidden = true;
      or.hidden = true;
    }
  }

  /* ------------------------------------------------------------- network */
  async function api(path, body) {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `Request failed (${res.status}).`);
    }
    return data;
  }

  async function refresh() {
    try {
      return await api("/api/auth/state");
    } catch (e) {
      return { ok: false, enabled: false };
    }
  }

  /* ---------------------------------------------------------- google flow */
  function pollGoogle(state, startedAt) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      if (Date.now() - startedAt > 240000) {
        clearInterval(pollTimer); pollTimer = null;
        busy(false);
        showError("That took too long. Try again.");
        return;
      }
      const auth = await refresh();
      if (auth.signed_in) { unlock(auth); return; }
    }, 1500);
  }

  $("#btn-google").addEventListener("click", async () => {
    showError("");
    busy(true, "Opening your browser…");
    try {
      const res = await api("/api/auth/google", {});
      if (!res.opened) {
        // Browser launch failed: let them open the page by hand.
        showError("Could not open your browser. Paste this address into it:\n" + res.url);
        busy(false);
        return;
      }
      busy(true, "Waiting for your browser…");
      pollGoogle(res.state, Date.now());
    } catch (e) {
      busy(false);
      showError(e.message);
    }
  });

  /* ---------------------------------------------------------- email flow */
  toggle.addEventListener("click", () => {
    mode = mode === "signin" ? "signup" : "signin";
    showError("");
    nameWrap.hidden = mode !== "signup";
    title.textContent = mode === "signup" ? "Create your account" : "Sign in to continue";
    subtitle.textContent = mode === "signup"
      ? "One account per person. Use any email address — no Google account needed."
      : "Your account ties your downloads to you, so problems can be fixed and you can be told when something fails.";
    toggle.textContent = mode === "signup" ? "I already have an account" : "Create an account";
    submit.textContent = mode === "signup" ? "Create account" : "Sign in";
    passEl.autocomplete = mode === "signup" ? "new-password" : "current-password";
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showError("");
    const email = emailEl.value.trim();
    const password = passEl.value;
    if (!email || !password) { showError("Enter an email and a password."); return; }
    busy(true, mode === "signup" ? "Creating…" : "Signing in…");
    try {
      const path = mode === "signup" ? "/api/auth/signup" : "/api/auth/signin";
      const payload = { email, password };
      if (mode === "signup") payload.name = nameEl.value.trim();
      const res = await api(path, payload);
      unlock(res.auth);
    } catch (err) {
      busy(false);
      showError(err.message);
      passEl.select();
    }
  });

  $("#auth-forgot").addEventListener("click", async () => {
    const email = emailEl.value.trim();
    if (!email) { showError("Type your email address first, then press this again."); return; }
    showError("");
    try {
      await api("/api/auth/reset", { email });
      showError("");
      errorBox.textContent = "";
      errorBox.hidden = false;
      errorBox.className = "auth-error ok";
      errorBox.textContent = `Password reset link sent to ${email}.`;
    } catch (err) {
      errorBox.className = "auth-error";
      showError(err.message);
    }
  });

  /* ------------------------------------------------------------- sign out */
  const signOut = document.getElementById("btn-signout");
  if (signOut) {
    signOut.addEventListener("click", async () => {
      try { await api("/api/auth/signout", {}); } catch (e) { /* ignore */ }
      location.reload();
    });
  }

  /* ---------------------------------------------------------------- boot */
  (async function init() {
    const auth = await refresh();
    if (!auth.enabled) {
      // No accounts configured: behave exactly as before.
      resolveReady();
      return;
    }
    if (auth.signed_in) {
      unlock(auth);
    } else {
      lock(auth);
    }
    // A 401 from any app call means the session died; show the gate again.
    window.addEventListener("auth-required", () => {
      if (!signedIn) return;
      signedIn = false;
      gate.hidden = false;
      document.body.classList.add("locked");
      showError("Your session expired. Please sign in again.");
    });
  })();

  window.Auth = {
    get signedIn() { return signedIn; },
    refresh,
    unlock,
    signOut: () => api("/api/auth/signout", {}),
  };
})();
