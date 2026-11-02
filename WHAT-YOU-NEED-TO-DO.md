# What you need to do

Everything below is the short, actionable version. Last updated after the admin
dashboard was rebuilt and deployed.

---

## 1. Your account

| | |
|---|---|
| **Owner email** | `brian.bcyang27@gmail.com` |
| **Password** | none — this account signs in with **Google** |
| **Where to sign in** | https://playlist-downloader-99ad0.web.app |

All existing accounts in Firebase Authentication (`brian.bcyang27@gmail.com`,
`stonytonybb@gmail.com`, `ohnapek@gmail.com`) were created with Google Sign-In,
so there is no password to type. Use **Continue with Google** and pick
`brian.bcyang27@gmail.com`.

Only `brian.bcyang27@gmail.com` can read the dashboard. The other accounts are
signed out immediately by the page and blocked again by Firestore rules.

> If you *want* a password as well: sign out in the desktop app → **Forgot
> password**, or Firebase Console → Authentication → Users → your user → **Reset
> password**. Use a long, unique password from a password manager. After that the
> “Sign in with email” box on the dashboard works too.

---

## 2. Check the admin dashboard

1. Open **https://playlist-downloader-99ad0.web.app**
2. Click **Continue with Google** → choose `brian.bcyang27@gmail.com`
3. You should see the dashboard with real data (as of this writing: **21
   download runs** and **11 errors**).

What you can do there:

- **Overview** — run counts, completed / partial / failed, tracks downloaded,
  users, unread errors, recent activity.
- **Downloads** — search and filter by status/platform, expand a row for the
  link, UID, timings and sample track names, export CSV.
- **Errors** — search, filter unread/reviewed, mark reviewed, export CSV.
- **Users** — per-user runs, tracks, platforms, OS and last activity.

If it says **“Firestore refused the read”**, you signed in with the wrong
Google account — sign out and use the owner email.

---

## 3. Check sign-in inside the desktop app

The app's **Sign in with Google** button used to point at a dead GitHub Pages
URL. It now opens the hosted page
**https://playlist-downloader-99ad0.web.app/login.html**, which hands the
session back to the app on `http://127.0.0.1:<port>`.

1. Launch the app.
2. Settings / Account → **Sign in with Google**.
3. Your browser opens the page → **Continue with Google** → pick the same
   account.
4. The browser says “Signed in as … Returning to the app”, and the app shows
   your account chip.

Signed-in runs are what appear in the dashboard.

---

## 4. GitHub is still blocked

`git push` fails with:

```
remote: Your account is suspended.
```

The `brianbcyangmusic-pixel` account is suspended, so the last commits exist
**only on this Mac** (latest: `1acf590`). Nothing is lost — the site is served
by Firebase Hosting, not GitHub.

You need to:

- [ ] Appeal / resolve the suspension at https://support.github.com
- [ ] Then run `git push origin main` from this folder
- [ ] (Optional) Re-enable GitHub Pages only if you want a second copy; the
      dashboard does not need it

If you decide to publish under a different GitHub account instead, change the
remote first:

```sh
git remote set-url origin https://github.com/<account>/playlist-downloader.git
git push -u origin main
```

---

## 5. Deploying changes later

Firebase CLI is installed and logged in as `brian.bcyang27@gmail.com`.

```sh
# after editing anything in admin/
firebase deploy --only hosting --project playlist-downloader-99ad0

# only when firestore.rules changes
firebase deploy --only firestore:rules --project playlist-downloader-99ad0
```

---

## 6. Where things live

| Path | What it is |
|---|---|
| `admin/index.html`, `admin/admin.js` | Owner dashboard (deployed to the site root) |
| `admin/login.html` | Browser page for the app's Google sign-in |
| `firebase.json`, `.firebaserc` | Firebase Hosting config (project `playlist-downloader-99ad0`) |
| `firestore.rules` | Deployed security rules (owner-only reads) |
| `cloud_config.json` | App's Firebase config, owner email, `login_url` |
| `app/telemetry.py` | Writes `downloads` / `errors` rows the dashboard reads |
| `web/` | The desktop app's UI, served locally by the Python server |

The Firebase **web API key** in these files is public by design — it identifies
the project but does not grant access. Access is decided by the Firestore rules
and the owner check on the page. No service-account key is in the repo or the app.
