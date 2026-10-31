"""Optional accounts, sign-in and usage logging.

This talks to **Firebase** (Authentication + Firestore) over its plain REST
API using only the standard library, so neither the app nor the build gains a
dependency — importantly, no Firebase SDK and no service-account key ever
ships inside the app.

Design notes
------------
* The Firebase *web* API key is public by design (it identifies the project,
  it does not authorise anything) — Firestore security rules decide what a
  signed-in user may read or write.
* Passwords are never stored locally. What we persist is the refresh token
  Firebase hands back, in a 0600 file inside the app's data folder, so the
  user stays signed in between launches.
* Nothing here is allowed to break the app: every network call is wrapped and
  returns ``{"ok": False, "error": ...}`` instead of raising. If the project is
  not configured, or the network is down, the app keeps working.
* Google sign-in goes through the **system browser** with a loopback redirect
  (the same pattern `gh auth login` uses). Google blocks OAuth inside embedded
  webviews, so this is the only supported route for a desktop app.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__, paths

CONFIG_NAME = "cloud_config.json"
SESSION_NAME = "cloud_session.json"
TIMEOUT = 15

_ENV_CONFIG = "PLAYLIST_DOWNLOADER_CLOUD"

#: Firebase's REST error codes are terse; these are what a person should see.
_FRIENDLY = {
    "EMAIL_EXISTS": "That email already has an account — try signing in instead.",
    "EMAIL_NOT_FOUND": "No account uses that email.",
    "INVALID_PASSWORD": "That password is not right.",
    "INVALID_LOGIN_CREDENTIALS": "That email and password do not match.",
    "INVALID_EMAIL": "That does not look like an email address.",
    "MISSING_PASSWORD": "Enter a password.",
    "WEAK_PASSWORD": "Firebase wants a longer password (at least 6 characters).",
    "PASSWORD_LOGIN_DISABLED": "Email sign-in is turned off for this project.",
    "OPERATION_NOT_ALLOWED": (
        "This sign-in method is not enabled in the Firebase console yet."
    ),
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Wait a minute and try again.",
    "USER_DISABLED": "That account has been disabled.",
    "INVALID_IDP_RESPONSE": "Google rejected the sign-in. Try again.",
}


def _friendly(message: str) -> str:
    code = (message or "").strip()
    for key, text in _FRIENDLY.items():
        if key in code:
            return text
    # "WEAK_PASSWORD : Password should be at least 6 characters"
    text = code.replace("_", " ")
    return (text[:1].upper() + text[1:]) if text else "Sign-in failed."


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def config_path() -> Path:
    return paths.app_root() / CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the Firebase project identifiers, or return an empty config.

    Precedence: ``PLAYLIST_DOWNLOADER_CLOUD`` (a JSON string, handy for CI) then
    ``cloud_config.json`` next to the app.
    """
    raw = os.environ.get(_ENV_CONFIG)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    path = config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    return (not value) or low.startswith(("paste", "your-", "xxx", "changeme"))


class Cloud:
    """Sign-in state plus the two collections the dashboard reads."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else load_config()
        self._lock = threading.RLock()
        self._session: dict[str, Any] = {}
        self._session_path = paths.data_dir() / SESSION_NAME
        self._load_session()

    # -- configuration -----------------------------------------------------
    @property
    def api_key(self) -> str:
        return str(self.config.get("api_key") or "")

    @property
    def project_id(self) -> str:
        return str(self.config.get("project_id") or "")

    @property
    def admin_email(self) -> str:
        return str(self.config.get("admin_email") or "").strip().lower()

    @property
    def configured(self) -> bool:
        return not _looks_like_placeholder(self.api_key) and bool(self.project_id)

    @property
    def enabled(self) -> bool:
        """Configured *and* not explicitly switched off."""
        return self.configured and bool(self.config.get("enabled", True))

    def _allowed(self, email: str) -> bool:
        """Optional allow-list: when set, only these emails may sign in."""
        allowed = [str(e).strip().lower() for e in (self.config.get("allowed_emails") or [])]
        if not allowed:
            return True
        return (email or "").strip().lower() in allowed

    # -- session persistence ----------------------------------------------
    def _load_session(self) -> None:
        if not self._session_path.is_file():
            return
        try:
            data = json.loads(self._session_path.read_text("utf-8"))
            if isinstance(data, dict) and data.get("refresh_token"):
                self._session = data
        except (OSError, json.JSONDecodeError):
            self._session = {}

    def _save_session(self) -> None:
        try:
            self._session_path.write_text(json.dumps(self._session), "utf-8")
            try:
                os.chmod(self._session_path, 0o600)
            except OSError:
                pass  # Windows: best effort
        except OSError:
            pass

    def _clear_session(self) -> None:
        self._session = {}
        try:
            self._session_path.unlink()
        except OSError:
            pass

    # -- state for the UI --------------------------------------------------
    def state(self) -> dict[str, Any]:
        email = str(self._session.get("email") or "")
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "signed_in": bool(self._session.get("refresh_token")),
            "user": (
                {
                    "uid": self._session.get("uid"),
                    "email": email,
                    "name": self._session.get("name") or "",
                    "verified": bool(self._session.get("verified")),
                }
                if self._session
                else None
            ),
            "google": bool(self.login_url),
            "admin": bool(self.admin_email and email.lower() == self.admin_email),
            "allowlist": bool(self.config.get("allowed_emails")),
        }

    # -- HTTP plumbing -----------------------------------------------------
    def _request(self, url: str, payload: dict[str, Any] | None = None,
                 method: str | None = None,
                 token: str | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            url, data=body, headers=headers, method=method or ("POST" if body else "GET")
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return {"ok": True, "data": json.loads(raw)}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                parsed = json.loads(exc.read().decode("utf-8") or "{}")
                detail = (parsed.get("error") or {}).get("message") or ""
            except Exception:
                pass
            return {"ok": False, "status": exc.code, "code": detail,
                    "error": _friendly(detail or f"HTTP {exc.code}")}
        except urllib.error.URLError as exc:
            return {"ok": False, "offline": True,
                    "error": f"Could not reach the server ({exc.reason})."}
        except (TimeoutError, OSError) as exc:
            return {"ok": False, "offline": True, "error": f"Network problem ({exc})."}

    def _auth_url(self, path: str) -> str:
        return f"https://identitytoolkit.googleapis.com/v1/{path}?key={self.api_key}"

    # -- sign-in -----------------------------------------------------------
    def _adopt(self, data: dict[str, Any], name_hint: str = "") -> dict[str, Any]:
        """Turn a Firebase token response into our stored session."""
        out = {
            "uid": data.get("localId") or data.get("user_id") or "",
            "email": data.get("email") or "",
            "name": data.get("displayName") or name_hint,
            "id_token": data.get("idToken") or data.get("id_token") or "",
            "refresh_token": data.get("refreshToken") or data.get("refresh_token") or "",
            "expires_at": time.time() + int(data.get("expiresIn") or data.get("expires_in") or 3600),
            "verified": bool(data.get("emailVerified", False)),
        }
        if not self._allowed(out["email"]):
            return {"ok": False, "error": "This app is not available for that account."}
        if not out["refresh_token"]:
            return {"ok": False, "error": "The server did not return a session."}
        with self._lock:
            self._session = out
            self._save_session()
        return {"ok": True, "user": self.state()["user"]}

    def sign_up(self, email: str, password: str, name: str = "") -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "error": "Accounts are not configured in this build."}
        res = self._request(self._auth_url("accounts:signUp"),
                            {"email": email, "password": password, "returnSecureToken": True})
        if not res["ok"]:
            return res
        data = res["data"]
        result = self._adopt(data, name)
        if result.get("ok") and name and data.get("idToken"):
            # Best effort: a pretty display name is not worth a failure.
            self._request(self._auth_url("accounts:update"), {
                "idToken": data["idToken"], "displayName": name, "returnSecureToken": False,
            })
            with self._lock:
                self._session["name"] = name
                self._save_session()
        return result

    def sign_in(self, email: str, password: str) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "error": "Accounts are not configured in this build."}
        res = self._request(self._auth_url("accounts:signInWithPassword"),
                            {"email": email, "password": password, "returnSecureToken": True})
        if not res["ok"]:
            return res
        return self._adopt(res["data"])

    def sign_in_with_google(self, id_token: str) -> dict[str, Any]:
        """Exchange a Google ID token for a Firebase session."""
        if not self.enabled:
            return {"ok": False, "error": "Accounts are not configured in this build."}
        res = self._request(self._auth_url("accounts:signInWithIdp"), {
            "postBody": f"id_token={id_token}&providerId=google.com",
            "requestUri": "http://localhost",
            "returnIdpCredential": True,
            "returnSecureToken": True,
        })
        if not res["ok"]:
            return res
        return self._adopt(res["data"])

    def reset_password(self, email: str) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "error": "Accounts are not configured in this build."}
        res = self._request(self._auth_url("accounts:sendOobCode"),
                            {"requestType": "PASSWORD_RESET", "email": email})
        return {"ok": True} if res["ok"] else res

    def sign_out(self) -> dict[str, Any]:
        with self._lock:
            self._clear_session()
        return {"ok": True}

    # -- browser sign-in handoff ------------------------------------------
    @property
    def login_url(self) -> str:
        """The hosted page that performs Google sign-in in a real browser."""
        url = str(self.config.get("login_url") or "")
        if _looks_like_placeholder(url):
            return ""
        return url

    def adopt_tokens(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept a session produced by the browser page.

        The page signs in with Google via Firebase and hands back Firebase's own
        tokens, so the app never needs an OAuth client secret or a registered
        redirect URI of its own.
        """
        if not self.enabled:
            return {"ok": False, "error": "Accounts are not configured in this build."}
        id_token = str(payload.get("id_token") or "")
        refresh = str(payload.get("refresh_token") or "")
        email = str(payload.get("email") or "")
        if not id_token or not refresh or not email:
            return {"ok": False, "error": "The sign-in page did not return a session."}
        if not self._allowed(email):
            return {"ok": False, "error": "This app is not available for that account."}
        try:
            expires = float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires = 3600.0
        with self._lock:
            self._session = {
                "uid": str(payload.get("uid") or ""),
                "email": email,
                "name": str(payload.get("name") or ""),
                "id_token": id_token,
                "refresh_token": refresh,
                "expires_at": time.time() + expires,
                "verified": bool(payload.get("verified", True)),
            }
            self._save_session()
        return {"ok": True, "user": self.state()["user"]}

    # -- tokens ------------------------------------------------------------
    def _refresh(self) -> dict[str, Any]:
        token = self._session.get("refresh_token")
        if not token:
            return {"ok": False, "error": "Not signed in."}
        res = self._request(
            f"https://securetoken.googleapis.com/v1/token?key={self.api_key}",
            {"grant_type": "refresh_token", "refresh_token": token},
        )
        if not res["ok"]:
            if res.get("status") in (400, 401, 403):
                with self._lock:
                    self._clear_session()
            return res
        data = res["data"]
        with self._lock:
            self._session.update({
                "id_token": data.get("id_token") or self._session.get("id_token"),
                "refresh_token": data.get("refresh_token") or token,
                "expires_at": time.time() + int(data.get("expires_in") or 3600),
                "uid": data.get("user_id") or self._session.get("uid"),
            })
            self._save_session()
        return {"ok": True}

    def token(self) -> str | None:
        """A usable ID token, refreshed when it is close to expiring."""
        if not self.enabled or not self._session.get("refresh_token"):
            return None
        if float(self._session.get("expires_at") or 0) - time.time() < 120:
            if not self._refresh().get("ok"):
                return None
        return self._session.get("id_token") or None

    # -- Firestore ---------------------------------------------------------
    @staticmethod
    def _encode(value: Any) -> dict[str, Any]:
        if value is None:
            return {"nullValue": None}
        if isinstance(value, bool):
            return {"booleanValue": value}
        if isinstance(value, int):
            return {"integerValue": str(value)}
        if isinstance(value, float):
            return {"doubleValue": value}
        if isinstance(value, (list, tuple)):
            return {"arrayValue": {"values": [Cloud._encode(v) for v in value]}}
        if isinstance(value, dict):
            return {"mapValue": {"fields": {k: Cloud._encode(v) for k, v in value.items()}}}
        return {"stringValue": str(value)}

    def _docs_url(self, collection: str, doc_id: str = "", suffix: str = "") -> str:
        base = (f"https://firestore.googleapis.com/v1/projects/{self.project_id}"
                f"/databases/(default)/documents/{collection}")
        if doc_id:
            base += f"/{urllib.parse.quote(doc_id, safe='')}"
        return base + suffix

    def write(self, collection: str, fields: dict[str, Any],
              doc_id: str | None = None, update: bool = False,
              anonymous: bool = False) -> dict[str, Any]:
        """Create (or patch) a document. Silent failure by design.

        ``anonymous`` is only for the sign-in failure case: by definition the
        person has no session yet, so there is no token to send. Security rules
        allow that create only for ``kind == 'auth_failed'`` documents.
        """
        if not self.enabled:
            return {"ok": False, "error": "Cloud logging is off."}
        token = self.token()
        if not token and not anonymous:
            return {"ok": False, "error": "Not signed in."}
        payload = {"fields": {k: self._encode(v) for k, v in fields.items()}}
        if update:
            if not doc_id:
                return {"ok": False, "error": "update needs a document id."}
            mask = "&".join(
                f"updateMask.fieldPaths={urllib.parse.quote(k)}" for k in fields
            )
            res = self._request(self._docs_url(collection, doc_id, f"?{mask}"),
                                payload, method="PATCH", token=token)
        else:
            url = self._docs_url(collection)
            if doc_id:
                url += f"?documentId={urllib.parse.quote(doc_id, safe='')}"
            res = self._request(url, payload, method="POST", token=token)
        if not res["ok"]:
            return {"ok": False, "error": res.get("error", "Could not save to the database.")}
        return {"ok": True, "id": doc_id or ""}

    # -- convenience -------------------------------------------------------
    def log_download(self, doc_id: str | None = None, **fields: Any) -> dict[str, Any]:
        fields.setdefault("created_at", _now_iso())
        fields.setdefault("app_version", __version__)
        return self.write("downloads", fields, doc_id)

    def log_auth_error(self, email: str, message: str) -> dict[str, Any]:
        """Record a failed sign-in. Works without a session, by design."""
        return self.write("errors", {
            "kind": "auth_failed",
            "message": str(message)[:400],
            "email": str(email)[:200],
            "uid": "",
            "user_email": str(email)[:200],
            "notified": False,
            "created_at": _now_iso(),
            "app_version": __version__,
        }, anonymous=True)

    def log_error(self, message: str, kind: str = "app_error",
                  **fields: Any) -> dict[str, Any]:
        payload = {"kind": kind, "message": str(message)[:2000],
                   "notified": False, "created_at": _now_iso(),
                   "app_version": __version__, **fields}
        return self.write("errors", payload)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
