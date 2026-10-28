# Verification — 2026-10-26

## Version 1.2.1 — 2026-10-28

- **84 tests passed**, including actual polling/delivery telemetry, safe error reporting and recovery, user-specific send failures, stopped-task health failures, watchdog heartbeat suppression on task death, optional-menu failure isolation, bounded Telegram concurrency, TLS verification/reuse, one inbound scan per monitoring cycle and native-heap maintenance.
- A second production stall occurred after memory grew beyond the adjusted soft limit. Increasing the limit alone did not solve the recurrence. The corrected runtime now returns unused native memory, shares verified CA stores and reduces repeated inbound-list allocation. The exact allocation responsible for the long-lived growth was not isolated: separate baseline tests with 400 HTTP reads, 400 verified synthetic HTTPS reads, 200 HTTPS error responses and 30 QR renders plateaued after warm-up. These tests used local synthetic servers, not production clients.
- systemd receives liveness notifications from the event loop itself. A frozen loop cannot keep sending healthy notifications. A stopped critical task suppresses watchdog heartbeats and makes `/health` return 503. Telegram connection state follows actual GetUpdates outcomes, rather than successful getMe alone. Known network/webhook conflicts remain visible and do not remove updates or webhooks.
- The isolated systemd watchdog probe deliberately freezes a separate test process, verifies automatic restart and then removes that test service. No production panel or Xray process is intentionally stopped during these tests. The live rollout checks bot telemetry, memory pressure, panel connectivity and preserved business records.

## Operational correction — 2026-10-27

The original 256 MiB soft memory threshold stalled the running Python process after its working set reached about 295 MiB. Linux reported `mem_cgroup_handle_over_high`, over 1.6 million high-memory events and approximately 84% memory stall time despite ample free host RAM. The app remained active while HTTP and Telegram handling stalled. The service limits were changed persistently to `MemoryHigh=448M`, `MemoryMax=512M`, leaving CPU and isolation controls in place. The same process recovered without restart, HTTP responded again and the nine pending Telegram updates drained to zero. No panel/Xray restart or client configuration change was needed. The service template now carries the corrected limits. Size the limits to the real working set, particularly when rendering receipt images and branded QR cards; a low soft threshold can stall a service without an OOM event.

## Version 1.2.0

- **74 tests passed.** Additional coverage includes concurrent direct-card approvals with unchanged wallet balance, a review hold that blocks expiration/cancellation/second payment, receipt rejection and resubmission, exact amount/ownership and duplicate-image checks, the first administrator photo notification with inline approve/reject actions, native checkout through photo upload, unauthorized approval rejection and provisioning/delivery after approval.
- Branded QR cards are decoded back to the exact subscription URL using ZXing, including after JPEG compression at quality 70 and for long subscription URLs. Branding remains outside the QR quiet zone. The decoder is a development dependency only.
- The full flow is exercised with Telegram and panel stubs; no real bank transfer or live receipt approval is performed as a test. Legacy balances and previous transactions are preserved. A fresh demo database also seeds successfully after the schema extensions.

## Version 1.1.1

- **63 tests passed.** Nine additional cases cover a single shared client across four inbounds, consecutive names after existing manual clients, partial-create and ambiguous-attach recovery, new-inbound synchronization without changing identity/quota, foreign-client isolation, concurrent durable name reservations, external naming races, paginated official API shapes, safe legacy-database migration and renewal without duplicate volume/time.
- All previous single-inbound behavior remains covered. Expired/cancelled invoices do not allocate a new sequential name; creation retries reuse the reserved name and renewals keep the existing service email.
- The all-inbounds option is exposed in both management web interfaces and the native administrator bot. Only active supported inbounds are automatically attached; existing memberships are never detached by reconciliation.
- Live verification repairs memberships only for identities already recorded in the shop database and verifies subscription link count, preserved credentials/quota/expiry, unchanged other customers, unchanged panel/Xray process IDs and the next available account name without creating a test customer.

## Version 1.1.0

- **54 tests passed** on Python 3.11. New coverage includes Telegram Mini App signature validation (including the optional signature field), expiration/tampering, customer/admin role isolation, CSRF/origin rejection, sessions without third-party cookies, revocation on ban, ownership of orders/tickets, receipt upload validation and duplicate handling, persistent shop settings, HTTPS configuration, inline-only bot menus, native administrator authorization and confirmation, plan creation, and seven animated Happ guide assets.
- Ruff, JavaScript syntax checks for all three Mini App modules and `pip check`: passed.
- Browser at 390 × 844: customer home, purchase/checkout and Happ guide; manager overview, plans, successful plan save and card/shop settings. The visible demo banner distinguishes sample records from production. No document-level horizontal overflow observed.
- Production deployment uses a separate loopback app and a bounded HTTPS gateway. Signed Telegram authentication is required publicly; public password-login routes and demo login are blocked. Panel database configuration and process IDs are compared against a protected baseline during deployment. No test customer is provisioned on the live panel.
- The seven Happ GIFs are schematic teaching assets, not recordings of the Happ UI. Official device download links were checked against Happ's official website and release repositories.

## Version 1.0.0 baseline

- Python 3.11: **35 tests passed** using pytest. Covers store transactions, concurrent wallet payments, order ownership, invoice cancellation/expiry, immutable installation currency, coupons, gifts, Stars settlement/duplicate refunds, receipts, referrals, provisioning/reconciliation, absolute renewal targets, remote disable before refund, outbox deduplication, API authentication/CSRF, protected persistent card configuration, CSV escaping, database backups and aiogram update dispatch.
- `ruff check farstar tests`: passed.
- `node --check farstar/static/app.js`: passed.
- `pip check`: no broken requirements.
- Editable package build/install: passed. Runtime dependencies are pinned in requirements.lock.
- `docker compose config --quiet`: passed. Container build/run was not tested because the local Docker daemon is not running.
- Browser: demo login succeeded; overview, orders, services, servers, plans, users, receipts, support, codes, reports and settings rendered. Plan form save succeeded. No JavaScript console errors were observed during page checks.
- Browser responsive check: 390 × 844 viewport; no document-level horizontal overflow. Desktop viewport restored after inspection.
- Local demo: http://localhost:8081, isolated sample database, external connections disabled. Dashboard screenshot: artifacts/dashboard.png.
- Upstream API checked against the official v3.11.0 OpenAPI, controller and model sources, including ClientRecord-to-Client type conversions. Automated HTTP integration tests use mocked transport; Telegram tests use an aiogram session stub. Real deployment separately verified the bot connection and read-only panel API. No live payment or paid customer account was created as a test.

One non-failing warning is emitted by the installed Starlette TestClient about a future HTTP client migration. It does not affect the passing tests or the application's production HTTP client.
