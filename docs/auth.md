# Users, Groups & Tokens

ZMM ships with a built-in identity system so the gateway can be safely
shared between household members and used by mobile apps without exposing
the whole API. It supports:

- **Username/password login** for the web UI (browser cookie session).
- **Bearer tokens** for programmatic access (curl, Android companion app, scripts).
- **Groups** to bundle scopes into reusable roles.
- **Scopes** to limit what each user, token, or group can do.

## Concepts

### Users

A human identity. Every login uses a username and (usually) a password.
Users can belong to zero or more groups, and additionally have direct
scope grants for fine-tuning.

### Groups

A named bundle of scopes. Default groups created on first run:

| Group     | Purpose                                                        |
|-----------|----------------------------------------------------------------|
| `admins`  | Full control. Has the implicit `admin` super-scope.            |
| `users`   | Day-to-day household members — can use devices and automations.|
| `viewers` | Read-only. Can see state, can't change anything.               |
| `mobile`  | For phone-issued tokens — minimal default scopes.              |

You can edit these or add your own from Settings → Users.

### Scopes

Permissions are expressed as dotted strings like `device:write` or
`presence:write:user`. Built-in scopes:

| Scope                  | Allows                                                   |
|------------------------|----------------------------------------------------------|
| `admin`                | Everything. Implies every other scope.                   |
| `device:read`          | View device state, configs, lists.                       |
| `device:write`         | Send commands, change settings.                          |
| `automation:read`      | View automations.                                        |
| `automation:write`     | Create, modify, delete automations.                      |
| `group:read`           | View Zigbee groups.                                      |
| `group:write`          | Modify Zigbee groups.                                    |
| `matter:read`          | View Matter nodes.                                       |
| `matter:write`         | Commission / remove / control Matter devices.            |
| `system:read`          | System status, telemetry, logs.                          |
| `system:write`         | Restart services, edit config, run upgrades.             |
| `presence:read`        | Read presence-user state.                                |
| `presence:write`       | Update **any** user's presence.                          |
| `presence:write:<id>`  | Update **only** the named user's presence (mobile-app token). |

Wildcards work at any segment: `device:*` matches all device permissions,
`presence:write:*` matches all per-user presence writes.

### Tokens

Long-lived bearer credentials. A token is owned by one user, can be a
subset of that user's scopes, has an optional expiry, and an optional
device-id label so you can revoke a stolen phone without affecting the
other devices the user owns.

Token plaintext is shown ONCE at issue time. Copy it immediately — ZMM
only stores its SHA-256 hash, so a forgotten token can't be recovered;
you'd need to revoke it and issue a new one.

## First run

On first boot, ZMM creates an `admin` user with a random password and
prints it to the logs at `WARNING` level. To find it:

```bash
podman logs zigee-matter-manager 2>&1 | grep -A3 "FIRST-RUN AUTH"
```

or

```bash
tail -n 100 -f /opt/.zigbee-matter-manager/logs/zigbee.log | grep -A3 "FIRST-RUN AUTH"
```

You'll see:

```
======================================================================
FIRST-RUN AUTH BOOTSTRAP
  Admin username: admin
  Admin password: 8sKr-X3yG2qN
  Change it via Settings → Users as soon as possible.
======================================================================
```

Log in with those credentials, then go to **Settings → Users** and:
1. Edit `admin` and set a password you'll remember.
2. Create a personal account for yourself in the `admins` group.
3. Disable the `admin` account (or leave it for break-glass).

## Adding household members

For each person:

1. Settings → Users → **New User**.
2. Username (e.g. `alice`), password, and add them to `users` (not `admins`).
3. They can now log in to the UI on their own devices.

## Issuing tokens for the mobile app

Each phone gets its own scoped token. Because the only thing the companion
app needs to do is report **its owner's** location, give it the narrowest
possible scope.

1. Settings → Users → Tokens tab → **Issue Token**.
2. Pick the user (e.g. `user`).
3. Label: e.g. "User's Pixel 8".
4. Device ID (optional but recommended): a stable identifier from the
   phone — the companion app shows this in its settings screen.
5. **Don't tick any built-in scope checkboxes.**
6. In the "custom scope" field, enter exactly:
   ```
   presence:write:user
   ```
   (replacing `user` with the user_id of the presence user this phone
   should report for — see [presence_users.md](presence_users.md)).
7. Optional expiry: 365 days is reasonable; the token can be revoked
   anytime regardless.
8. Click Issue. The plaintext token appears once — copy it into the
   phone's app.

If the phone is later lost or the person leaves the household, revoke the
token from the same screen. The phone loses access immediately on its
next request.

## Self-service

Non-admin users see a stripped-down Settings → Tokens screen where they
can:
- Change their own password.
- Issue tokens for themselves (within the scope of their groups).
- Revoke their own tokens.

They cannot see other users' tokens, change groups, or modify the user
list.

## Soft mode (for migration)

If you have an existing ZMM install with scripts or homemade integrations
that hit unauthenticated endpoints, set `enforce=False` in the
`AuthMiddleware(...)` constructor in `main.py`. The middleware will log
warnings on anonymous requests but not block them, giving you time to
audit your scripts and add `Authorization: Bearer ...` headers. Switch
back to `enforce=True` once you're confident.

## Security notes

- **Passwords** are stored as PBKDF2-HMAC-SHA256, 200 000 iterations,
  per-password 16-byte salt.
- **Tokens** are stored as SHA-256 hashes. Plaintext exists only on the
  client (or briefly in the issue response).
- **Session cookies** are HMAC-SHA256-signed with a secret derived from
  the auth file's inode + mtime. Replacing the file (e.g. backup
  restore) invalidates all sessions — by design.
- **No JWT, OIDC, or OAuth** is used. Tokens are static until revoked
  or expired. This is appropriate for a home gateway; it would not be
  appropriate for a multi-tenant SaaS.
- **TLS** is your responsibility. The session cookie is set with
  `httponly` and `samesite=lax` but **not** `secure` because ZMM may
  be deployed over plain HTTP on a LAN. If you expose ZMM beyond your
  LAN, enable HTTPS in the existing web SSL settings — without it,
  bearer tokens and cookies are visible to anyone on the wire.

## Backup & restore

`auth.yaml` is included in ZMM backups by default. Restoring a backup
restores users, groups, and tokens — but invalidates all session cookies
since the file's inode changes. Existing bearer tokens continue to work.