# Security: MFA, Lockout, and LAN-Only Accounts

ZMM's authentication system supports:

- **TOTP-based two-factor authentication** for any user account.
- **Recovery codes** for the case where someone loses their phone.
- **Brute-force protection** with per-account lockout and per-IP rate limiting.
- **LAN-only accounts** for low-friction "kid's tablet" style access.
- **Trusted-proxy support** for Cloudflare Tunnel and other reverse proxies.

## Two-factor authentication (TOTP)

### Enabling it for yourself

1. Sign in to ZMM and go to **Settings → My Account**.
2. Click **Enable Two-factor Authentication**.
3. Scan the QR code with your authenticator app:
    - Google Authenticator
    - Authy
    - 1Password
    - Bitwarden
    - Microsoft Authenticator
    - Or any other TOTP-compatible app.
4. Type the 6-digit code your app shows to confirm.
5. **Save the recovery codes that appear next** — they are shown once and
   you'll need them if you lose your phone.

After this, every login asks for the 6-digit code as a second step.

### Recovery codes

Ten single-use codes, generated when you enable MFA. Each one can stand
in for a TOTP code exactly once — useful if your phone is dead, lost, or
out of sync.

If you've used most of them, regenerate a fresh set from **Settings →
My Account → Regenerate recovery codes**. The previous set is invalidated.

### Disabling MFA

From **Settings → My Account → Disable 2FA**. You'll be asked for your
password again as a confirmation. Recovery codes are wiped at the same
time.

### "I lost both my phone and my recovery codes"

You'll need shell access to ZMM to reset:

```bash
podman exec zmm python3 -c "
    from modules.auth import AuthManager
    from modules.auth_secure import SecureAuthManager
    import asyncio
    a = AuthManager(); a.load()
    s = SecureAuthManager(a)
    asyncio.run(s.disable_mfa('your-username'))
    print('MFA disabled — log in with password and re-enrol')
"
```

If you're not the only admin, ask another admin to:
**Settings → Users → \[your account\] → Edit → Disable MFA**.

## Brute-force protection

Login attempts are tracked per username and per source IP. Failed
attempts trigger increasing lockouts:

| Failures | Lockout       |
|----------|---------------|
| 3        | 1 minute      |
| 5        | 5 minutes     |
| 8        | 15 minutes    |
| 12+      | 1 hour (cap)  |

A successful login clears the failure counter. Lockouts are
**in-memory** — restarting the container clears them. This is
intentional: most attacks happen within minutes, and persisting
lockouts across restarts has more downsides (legitimate user gets
permanently stuck if they forget once and ZMM stays running for months)
than upsides.

There's also a per-IP rate limit: 30 attempts per 5 minutes from any
single IP, regardless of which username they're targeting. This
catches username-spraying attacks where an attacker tries one password
against every account.

### Admin unlock

If a household member legitimately gets locked out, an admin can clear
their lockout from **Settings → Users → Admin → Locked accounts**.

## LAN-only accounts

The `network:lan_only` scope can be added to any user. When set,
that user's login is rejected from any IP outside the configured LAN
ranges — even if their password and MFA were correct.

### When to use this

- A kid's tablet that should never need remote access.
- A shared "guest" account on a wall-mounted dashboard.
- Service accounts used by other home automation tools on the LAN.

These accounts can use a simple password without MFA. The threat model
is "someone on my home Wi-Fi" — and someone with that level of access
already has bigger concerns than a kid's account.

### How LAN is detected

By default, ZMM treats these as LAN:

- `127.0.0.0/8` — loopback
- `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` — RFC1918
- `169.254.0.0/16` — IPv4 link-local
- `100.64.0.0/10` — CGNAT (covers Tailscale)
- `::1`, `fc00::/7`, `fe80::/10` — IPv6 equivalents

You can override this in `config.yaml` under `security.network.lan_ranges`.

## Behind a reverse proxy (Cloudflare Tunnel, nginx, Caddy)

When ZMM sits behind a proxy, every request looks like it came from the
proxy itself. We need to tell ZMM which proxies it can trust to report
the real client IP.

### Cloudflare Tunnel

This is the recommended external-access path. To configure:

```yaml
security:
  network:
    cloudflare_tunnel_enabled: true
    trusted_proxies:
      - "127.0.0.0/8"   # cloudflared running locally
```

ZMM will then read the real client IP from `CF-Connecting-IP` headers
(but only when they arrive from a Cloudflare IP or your localhost
cloudflared process).

A dedicated [Cloudflare Tunnel setup guide](cloudflare_tunnel.md) is
shipped separately.

### Other reverse proxies (nginx, Caddy, Traefik)

Add the proxy's IP to `trusted_proxies`:

```yaml
security:
  network:
    trusted_proxies:
      - "127.0.0.0/8"
      - "10.0.0.5/32"   # your reverse-proxy host
```

Make sure your proxy sets `X-Forwarded-For` correctly. nginx example:

```nginx
location / {
    proxy_pass http://zmm:8000;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Host $host;
}
```

### Why this matters

Without configured trusted proxies, an attacker could send
`X-Forwarded-For: 192.168.1.5` to bypass the LAN-only check. ZMM
**ignores** these headers from any IP not in the trusted list,
specifically to prevent this attack.

## Token-based access (mobile app, scripts)

Bearer tokens **bypass the MFA prompt** by design — they ARE a second
factor of sorts, since they're long random strings stored on a specific
device. This matches how every other API service works (GitHub PATs,
AWS access keys, etc.).

Tokens inherit the LAN-only restriction from their owning user, however.
A token issued for a `network:lan_only` account can only be used from
the LAN.

## Logging

Watch for these log lines (`WARNING` level):

```
[bruteforce] sean locked for 60s after 3 failures
[bruteforce] IP 203.0.113.99 rate-limited
[network] kid attempted login from non-LAN (8.8.8.8) but holds network:lan_only
[network] CF-Connecting-IP from untrusted peer 203.0.113.66 — ignoring
[mfa] sean used a recovery code (9 remaining)
```

The last one in particular — recovery code usage — is worth noticing.
If you see it and you didn't expect it, change your password and
regenerate recovery codes immediately.