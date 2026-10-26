# Verification — 2026-10-26

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
