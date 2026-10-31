# Google sign-in

How sign-in works, how to switch it on, and how to run it.

## How it works

With `TCP_AUTH_ENABLED=true`:

1. The page asks the API whether sign-in is on (`GET /api/v1/auth/config`) and
   whether this browser already has a session (`GET /api/v1/auth/me`). If it
   does, the app opens without asking again.
2. Otherwise the user clicks **Sign in with Google**. Google's popup gives the
   page an access token, which the page posts once to `POST /api/v1/auth/google`.
3. The server asks Google's tokeninfo endpoint about the token itself. It
   accepts it only if:
   - `aud` and `azp` are **our** client id (a token from any other Google app
     is refused);
   - it has not expired;
   - the email is present and verified;
   - the email is allowed (`TCP_AUTH_ALLOWED_DOMAINS` / `_EMAILS`);
   - the account is not disabled.
4. The server creates a session: a random 256-bit token sent as the cookie
   `tcp_session` (HttpOnly, Secure, SameSite=Lax). The database stores only its
   SHA-256, so a leaked row cannot be used as a cookie.
5. Every later request, including image and download links, carries the cookie.
   The server looks it up and uses that email to scope projects.
   `X-User-Email`, `X-User-Id` and `?user=` are ignored.

Other rules:

- **Lifetime.** A session lasts `TCP_SESSION_TTL_HOURS` (7 days) since it was
  last used. Signing out deletes it on the server. `users.disabled = true` cuts
  off all of that account's sessions on their next request.
- **CSRF.** Writes that carry the cookie must also carry `X-Requested-With`,
  which the page always sends. Another site cannot add that header without a
  CORS preflight, and the preflight only succeeds for origins in
  `TCP_CORS_ORIGINS`. Reads need nothing, so `<img src>` and downloads work.
- **Airflow callbacks** are unchanged: they authenticate with
  `TCP_COMPUTE_TOKEN` (`X-Service-Token`) and never use the cookie.
- **Sign-in off** (`TCP_AUTH_ENABLED=false`) keeps the old behaviour for
  development: with a client id the page signs in and sends the email as
  `X-User-Email`, which the server trusts without checking.

Code: `code/app/services/auth.py` (verification, sessions),
`code/app/api/v1/auth.py` (routes), `code/app/api/deps.py` (`require_user`,
`require_api_key`, `require_app_request`), `frontend/index.html` (search for
`loadAuthConfig`).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TCP_AUTH_ENABLED` | `false` | `true` requires a verified sign-in on every user route. |
| `TCP_GOOGLE_CLIENT_ID` | — | Web client id (`…apps.googleusercontent.com`). Public. Required when sign-in is on; the server will not start without it. |
| `TCP_AUTH_ALLOWED_DOMAINS` | blank | Comma-separated. Blank (with the next one blank too) lets any verified Google account in. |
| `TCP_AUTH_ALLOWED_EMAILS` | blank | Individual addresses allowed in addition to the domains. |
| `TCP_SESSION_TTL_HOURS` | `168` | Session length since last use. |
| `TCP_COOKIE_SECURE` | `true` | `false` only for plain-http use on localhost. |
| `TCP_COOKIE_SAMESITE` | `lax` | `none` only when the page and API are on different *sites*; requires Secure. |
| `TCP_COOKIE_PATH` | `/` | Narrow to the app's path prefix when the host is shared (see below). |
| `TCP_CORS_ORIGINS` | `*` | Name the page's origin when the page is on another host than the API. |

## Switching it on

1. **Google Cloud Console → APIs & Services → Credentials → Create OAuth client
   ID → Web application.** Authorized JavaScript origins: the address users
   open, scheme and host only (`https://your-site.example`). Redirect URIs:
   none.
2. **OAuth consent screen:** scopes `openid` and `email`, then **Publish**.
   While it is in "Testing" only the listed test users can sign in.
3. **`.env`:**
   ```bash
   TCP_AUTH_ENABLED=true
   TCP_GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
   TCP_AUTH_ALLOWED_DOMAINS=           # e.g. iitd.ac.in,gmail.com, or blank for anyone
   ```
4. **Outbound access to Google.** The API must reach
   `https://oauth2.googleapis.com`. On the IIT Delhi network set the proxy for
   the API container (`HTTPS_PROXY=http://proxy21.iitd.ac.in:3128`, with
   `NO_PROXY` covering the internal hosts).
5. **Database:** Postgres needs `docker compose run --rm api alembic upgrade head`
   (migration `0003` adds `users` and `auth_sessions`). SQLite creates the
   tables itself.
6. **Restart** the API. No image rebuild is needed.
7. **Move old projects** to their owner. Projects created while sign-in was off
   belong to `default`, which nobody signs in as:
   ```bash
   docker compose exec api python scripts/reassign_owner.py --from default --to you@gmail.com          # dry run
   docker compose exec api python scripts/reassign_owner.py --from default --to you@gmail.com --apply
   ```
   Sign-in stores emails lower-cased. If older projects were stored under a
   mixed-case address, `scripts/reassign_owner.py --lowercase --apply` fixes
   them.

**Rollback:** `TCP_AUTH_ENABLED=false`, restart. Sessions in the database are
simply unused.

## Where to host it

**Best: its own origin.** A dedicated host or subdomain (e.g.
`treecrown.example.org`) proxied straight to the API. Nothing else is needed:

```nginx
server {
    listen 443 ssl;
    server_name treecrown.example.org;
    client_max_body_size 8g;
    proxy_read_timeout 3600s;
    proxy_request_buffering off;
    location / {
        proxy_pass http://127.0.0.1:8200;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Under a path on a shared host** (e.g. `https://www.cse.iitd.ernet.in/treecrown/`):
set `TCP_FRONTEND_API_BASE=https://www.cse.iitd.ernet.in/treecrown` and
`TCP_COOKIE_PATH=/treecrown`, so other apps on that host never receive the
session cookie. Be aware of the limit of this layout: every page on that
same origin is trusted by the browser as if it were this app's page, so a
script on any other page of that host could act as a signed-in user. Use a
dedicated subdomain when the host serves pages you do not control.

**Page on one host, API on another:** set the API URL in the page's config
block, `TCP_CORS_ORIGINS` to the page's exact origin (credentials are only
allowed for named origins, never `*`), and add that origin to the OAuth
client. Subdomains of one site keep `SameSite=lax`. Entirely different sites
need `TCP_COOKIE_SAMESITE=none`, which Safari and browsers blocking
third-party cookies will refuse, so prefer one origin.

## Operations

```sql
-- shut an account out (takes effect on its next request)
UPDATE users SET disabled = true WHERE email = 'someone@gmail.com';
-- sign everyone out
DELETE FROM auth_sessions;
-- who has signed in
SELECT email, created_at, last_login_at FROM users ORDER BY last_login_at DESC;
```

The activity log (`<storage_root>/activity/activity-YYYY-MM-DD.jsonl`) records
the verified email and Google account id for every write, including sign-in
and sign-out.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Popup error `origin_mismatch` | The site's address is missing from the OAuth client's JavaScript origins. |
| "This sign-in was not issued for this site." | The page and the server use different client ids. |
| 503 "could not reach Google" | No outbound https to `oauth2.googleapis.com`; set `HTTPS_PROXY`. |
| Signed in, then straight back to the sign-in screen | The cookie is not coming back: plain http with `TCP_COOKIE_SECURE=true`; `TCP_COOKIE_PATH` not covering the API path; a cross-origin page not in `TCP_CORS_ORIGINS`. |
| 403 `ACCOUNT_NOT_ALLOWED` | The email is outside `TCP_AUTH_ALLOWED_DOMAINS` / `_EMAILS`. |
| 403 `CSRF_REJECTED` | A script or tool wrote with the cookie but without `X-Requested-With`. |
| Server will not start | `TCP_AUTH_ENABLED=true` without a valid `TCP_GOOGLE_CLIENT_ID`, or `SameSite=none` without Secure. The error says which. |
| Old projects missing | They belong to `default`. Run `scripts/reassign_owner.py`. |
