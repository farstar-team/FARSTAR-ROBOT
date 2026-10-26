# Mini Apps and native administration — 1.1.0

## Telegram

Configure both URLs on a trusted HTTPS hostname:

```dotenv
MINI_APP_URL=https://shop.example.com/app
ADMIN_MINI_APP_URL=https://shop.example.com/admin-app
```

Restart only Farstar. The bot sets a global customer Web App menu and a per-chat manager menu for configured administrators. `/start` presents inline buttons and removes the legacy reply keyboard once per customer. `/admin` opens native administration, including plans, users, receipts, tickets, services, card details, trial/referral settings, server sales switches, coupons/gifts, broadcasts and private backups.

Mini Apps validate Telegram's signed `initData` using the bot token, reject stale/future authentication and check administrator IDs on every request. Customer records are filtered by ownership. Sessions have a two-hour maximum age, CSRF protection and exact-origin checks. An in-memory session header supports Telegram Web with blocked third-party cookies. The application never trusts `initDataUnsafe` for authentication. Demo login exists only in loopback demo mode.

Customer flow: choose plan, accept shop rules, create order, transfer the stated amount to the configured card, upload an image, wait for administrator review, then pay the order from the credited wallet. An uploaded image alone never credits the balance. Receipt requests, ledger entries and order payment have duplicate protections. The manager sees and confirms the receipt before credit. Renewal preserves the existing subscription identity and carries the specified additional volume and time.

## Separate HTTPS gateway

The app should bind only `127.0.0.1:18080`. An independent Caddy service can bind an unused HTTPS port and reverse-proxy the Mini Apps without restarting or editing 3x-ui/Xray. Before installation, check ports, record running process IDs and back up both application and panel databases. Do not replace existing listeners or share the panel database with Farstar.

Templates: [Caddy configuration](Caddy-miniapp.example), [gateway unit](farstar-miniapp-gateway.service). The gateway blocks public password-login endpoints. The dashboard remains accessible through an SSH tunnel; public administration requires a valid manager Telegram session. Unauthorized API requests are rejected by the backend.

Use a dedicated unprivileged gateway user, an official checksum-verified Caddy binary and CPU/memory limits. Set `MINI_HOST` in a root-owned gateway environment file. Copy the matching existing certificate/key into `/etc/farstar-miniapp/tls/` without changing the originals. The directory should be `root:farstargw 0750`, the copies `root:farstargw 0640`. Set `CREDENTIALS_DIRECTORY=/etc/farstar-miniapp/tls`. Arrange a root-owned renewal hook/path watcher that atomically refreshes those copies from the original certificate paths and restarts **only the gateway**. Never make the original panel key world-readable. Certificate renewal remains owned by the existing certificate setup.

Deploy a new root-owned release directory and atomically switch the `current` symlink. Preserve the old release and database backup for rollback. Restart only Farstar, then check signed authentication, HTTPS, approved plan values, app health, unchanged panel/Xray process IDs and stable client/inbound configuration fingerprints.

## Happ guides

The seven GIFs are schematic tutorials with Persian captions, not recordings of a particular Happ release. Download links point to the official stores and Happ-proxy release repositories. `scripts/build_happ_guides.py` regenerates the assets; optional authoring dependencies are in `.[guides]`. They are not required at runtime. TV guides cover importing via the official TV Web Import service; unsupported devices are not described as supported.
