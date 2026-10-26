# Mini Apps and native administration — 1.1.0

## Telegram

Configure both URLs on a trusted HTTPS hostname:

```dotenv
MINI_APP_URL=https://shop.example.com/app
ADMIN_MINI_APP_URL=https://shop.example.com/admin-app
```

Restart only Farstar. The bot sets a global customer Web App menu and a per-chat manager menu for configured administrators. `/start` presents inline buttons and removes the legacy reply keyboard once per customer. `/admin` opens native administration, including plans, users, receipts, tickets, services, card details, trial/referral settings, server sales switches, coupons/gifts, broadcasts and private backups.

Mini Apps validate Telegram's signed `initData` using the bot token, reject stale/future authentication and check administrator IDs on every request. Customer records are filtered by ownership. Sessions have a two-hour maximum age, CSRF protection and exact-origin checks. An in-memory session header supports Telegram Web with blocked third-party cookies. The application never trusts `initDataUnsafe` for authentication. Demo login exists only in loopback demo mode.

Customer flow: choose plan, accept shop rules, create order, transfer the exact invoice amount to the configured card and upload an image bound to that order. The administrator receives the photo with approve/reject buttons immediately. Approval atomically marks the order paid, without any wallet credit or second customer payment. The worker provisions it and sends a branded personal QR image with the subscription link in its caption. Orders awaiting receipt review do not expire or accept a second payment. Rejection reopens the same invoice for a new receipt. Receipt requests, image identifiers and order payment have duplicate protections. Renewal preserves the existing identity. Existing wallet credit, gifts and referral rewards remain usable; manual wallet top-ups are removed. Failed card purchases require a manual bank refund and do not automatically credit the wallet.

## Separate HTTPS gateway

The app should bind only `127.0.0.1:18080`. An independent Caddy service can bind an unused HTTPS port and reverse-proxy the Mini Apps without restarting or editing 3x-ui/Xray. Before installation, check ports, record running process IDs and back up both application and panel databases. Do not replace existing listeners or share the panel database with Farstar.

Templates: [Caddy configuration](Caddy-miniapp.example), [gateway unit](farstar-miniapp-gateway.service). The gateway blocks public password-login endpoints. The dashboard remains accessible through an SSH tunnel; public administration requires a valid manager Telegram session. Unauthorized API requests are rejected by the backend.

Use a dedicated unprivileged gateway user, an official checksum-verified Caddy binary and CPU/memory limits. Set `MINI_HOST` in a root-owned gateway environment file. Copy the matching existing certificate/key into `/etc/farstar-miniapp/tls/` without changing the originals. The directory should be `root:farstargw 0750`, the copies `root:farstargw 0640`. Set `CREDENTIALS_DIRECTORY=/etc/farstar-miniapp/tls`. Arrange a root-owned renewal hook/path watcher that atomically refreshes those copies from the original certificate paths and restarts **only the gateway**. Never make the original panel key world-readable. Certificate renewal remains owned by the existing certificate setup.

Deploy a new root-owned release directory and atomically switch the `current` symlink. Preserve the old release and database backup for rollback. Restart only Farstar, then check signed authentication, HTTPS, approved plan values, app health, unchanged panel/Xray process IDs and stable client/inbound configuration fingerprints.

## Happ guides

The seven GIFs are schematic tutorials with Persian captions, not recordings of a particular Happ release. Download links point to the official stores and Happ-proxy release repositories. `scripts/build_happ_guides.py` regenerates the assets; optional authoring dependencies are in `.[guides]`. They are not required at runtime. TV guides cover importing via the official TV Web Import service; unsupported devices are not described as supported.
