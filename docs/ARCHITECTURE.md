# Architecture and compatibility

## Runtime

One Python process hosts FastAPI, aiogram polling, a durable provisioning worker and an outbox worker on one event loop. SQLite transactions use BEGIN IMMEDIATE, foreign keys, a busy timeout and WAL. HTTP and Telegram calls are asynchronous; backups run in a thread. Run exactly one process/replica. Each server row represents one existing panel/inbound and its subscription base URL. Multiple rows can target one panel.

The HTTP application uses random server-side sessions, hashed session tokens, a CSRF token on mutations, strict SameSite/HttpOnly cookies, response security headers and no external frontend runtime. Set secure cookies and TLS on hosted deployments. Login/Telegram rate limits persist across restarts. API credentials are encrypted using Fernet with a key derived from SECRET_KEY. Service credentials and financial records remain in the database; protect backups and filesystem access.

## Upstream contract

Target: [MHSanaei/3x-ui v3.11.0](https://github.com/MHSanaei/3x-ui/tree/v3.11.0), inspected 2026-10-26. Reference source: [OpenAPI](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/docs/public/openapi.json), [client controller](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/internal/web/controller/client.go), [models](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/internal/database/model/model.go).

| Purpose | Method and path relative to webBasePath |
|---|---|
| Session auth fallback | POST /login, JSON username/password |
| Validate inbound | GET /panel/api/inbounds/get/{id} |
| Lookup | GET /panel/api/clients/get/{email} |
| Create | POST /panel/api/clients/add, JSON {client, inboundIds} |
| Full client update | POST /panel/api/clients/update/{email} |
| Native share links | GET /panel/api/clients/links/{email} |

Bearer tokens take precedence over session login. A monitor token cannot manage clients; an admin token is needed. HTTPS certificate validation stays enabled. Base paths are preserved, redirects are rejected, and authentication errors never mean “client absent”. Only the documented record-not-found response permits a create.

GET returns a wrapper with client, inboundIds and usedTraffic. Its ClientRecord differs from the write model: id is numeric and uuid holds the UUID; allowedIPs is CSV and reverse is serialized JSON. `client_payload` converts those fields to Client before updates and preserves the other known fields (flow, secrets, host rules, limits, reset schedules, etc.). Go's update endpoint replaces fields rather than patching them. No inbound update, whole-settings replacement or traffic reset is used for renewal.

New purchases support VLESS, VMess, Trojan, Shadowsocks, Hysteria and TUIC inbounds with standard panel subscriptions. Credentials are read back after create because the panel may generate/normalize protocol secrets, notably Shadowsocks 2022 keys. Share-link availability depends on the panel protocol; the subscription remains the primary delivery artifact. Existing inbounds must already be correctly configured, with reachable subscription and connection addresses. Node application may be asynchronous according to the upstream panel's own reconciliation.

## Orders and money

1. Create a pending invoice with a snapshot of price, duration, volume, deterministic email, UUID and random subId. Reserve coupon and server capacity transactionally. At most three unpaid invoices per user.
2. A wallet payment atomically debits the balance, inserts a unique ledger reference and marks the order paid. Repeated payment calls return without another debit.
3. Telegram pre-checkout validates owner, currency, amount, status and expiry, then exclusively reserves checkout. Only successful_payment settles money. Each charge ID is unique. Duplicate or mismatched additional charges are queued for a Telegram refund.
4. A worker leases paid orders. The target client payload is persisted before the remote operation. For renewals the new expiry/quota are absolute targets, so retrying after a timeout cannot append the purchase twice.
5. Every attempt looks up the same email/subId first and verifies inbound membership and the resulting limits. An ambiguous create is reconciled without generating another identity.
6. A transaction records the service, completion, first-purchase referral reward and delivery outbox entry. Repeated completion cannot duplicate the reward or delivery record.

An order retries five times with backoff, then remains failed for admin inspection. It retains its payment and capacity reservation. Admin retry reuses the original target; it never creates a second debit. Failed purchase refunds first disable a potentially created remote client and verify that it is disabled. Refunds are refused when the panel cannot be checked, or for renewals. Stars refunds retry through the worker; CHARGE_ALREADY_REFUNDED reconciles a crash after the remote refund.

Notification delivery is at least once. The outbox deduplicates event creation, but a crash between Telegram accepting a message and SQLite recording delivery can repeat a notification. It cannot repeat a wallet debit or provision a new client identity. Telegram network failures back off; blocked chats and invalid messages stop retrying. Users can always retrieve provisioned services from their menu independently of a notification.

## Operations and limits

- Currency is installation-wide. Choose before recording business data; migration between currencies is not supplied.
- Money uses integer units; volume uses binary GiB and expiry milliseconds at the API boundary.
- Service dates are displayed in Asia/Tehran; daily chart buckets use Tehran's UTC+03:30 offset.
- Coupons reserve uses through invoice expiry. Gift claims are unique per code/user. Trial claims are single-use per Telegram identity; this is not proof of a distinct human.
- Manual receipt approval is an administrator assertion, not bank verification. Never approve an image without matching the actual bank transaction.
- SQLite snapshots, .env/SECRET_KEY and panel state are separate recovery assets. Restoring only the bot database does not roll back Xray or panel changes.
- Admin changes to a client's quota while its renewal is processing can conflict with the saved renewal target; inspect the order before editing the same client in both systems.
- There is no automatic upgrade of the panel or its software. Inspect upstream API changes and re-run contract and staging tests before upgrading beyond the documented target.
- `/health` reports HTTP/database availability. Authenticated `/api/meta` exposes bot startup status and the worker's last cycle. Check these alongside logs and pending/failed orders for operational readiness.
