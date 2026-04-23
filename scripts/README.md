## Upgrading

The Settings tab includes an **Upgrade** section that pulls new tags from GitHub, builds a new container image in the background, and atomically swaps when you're ready.

### How it works

1. The app polls the GitHub Releases API every 6 hours for new stable tags.
2. When a new version is found, a banner appears in Settings → Upgrade.
3. Clicking **Build** triggers a host-side script that clones the tag, builds a new image tagged `zigbee-matter-manager:<version>-<arch>`, and leaves it ready to swap. Your app keeps running during the build (~15–25 min).
4. Clicking **Swap now** stops the old container, starts the new one, and runs a health check. If the new container doesn't respond within 60 seconds, the swap is automatically rolled back.
5. The previous container and image are retained for one-click rollback.

### First-time setup

The upgrade feature requires a small host-side watcher. On fresh installs from `build.sh` it's installed automatically. To install on an existing deployment:

```bash
curl -fsSL https://raw.githubusercontent.com/oneofthemany/ZigBee-Matter-Manager/main/scripts/install_watcher.sh | bash
```

The watcher uses `systemd-path` units where available (event-driven, no CPU when idle) and falls back to a polling loop on systems without systemd.

### Auto-update

Off by default. When enabled, updates are only installed during the configurable quiet window (default 03:00–05:00) so restarts don't happen while you're using the heating or adjusting devices.

### Rollback

After any successful upgrade, the previous image and a stopped container named `zigbee-matter-manager-previous` are kept. Click **Rollback** in Settings → Upgrade to swap back in ~15 seconds.

### Image retention

Old images are kept per the retention setting (default: 2 most recent). Each image is roughly 1.5–2 GB, so don't set this too high on space-constrained devices like the Rock 5B. Use the **Clean up old images** button to prune manually.