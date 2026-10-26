# Verification — 2026-10-26

- Python 3.11: **35 tests passed** using pytest. Covers store transactions, concurrent wallet payments, order ownership, invoice cancellation/expiry, immutable installation currency, coupons, gifts, Stars settlement/duplicate refunds, receipts, referrals, provisioning/reconciliation, absolute renewal targets, remote disable before refund, outbox deduplication, API authentication/CSRF, protected persistent card configuration, CSV escaping, database backups and aiogram update dispatch.
- `ruff check farstar tests`: passed.
- `node --check farstar/static/app.js`: passed.
- `pip check`: no broken requirements.
- Editable package build/install: passed. Runtime dependencies are pinned in requirements.lock.
- `docker compose config --quiet`: passed. Container build/run was not tested because the local Docker daemon is not running.
- Browser: demo login succeeded; overview, orders, services, servers, plans, users, receipts, support, codes, reports and settings rendered. Plan form save succeeded. No JavaScript console errors were observed during page checks.
- Browser responsive check: 390 × 844 viewport; no document-level horizontal overflow. Desktop viewport restored after inspection.
- Local demo: http://localhost:8081, isolated sample database, external connections disabled. Dashboard screenshot: artifacts/dashboard.png.
- Upstream API checked against the official v3.11.0 OpenAPI, controller and model sources, including ClientRecord-to-Client type conversions. HTTP integration tests use mocked transport; Telegram tests use an aiogram session stub. No real panel, Telegram bot token or financial transaction was available for an end-to-end production test.

One non-failing warning is emitted by the installed Starlette TestClient about a future HTTP client migration. It does not affect the passing tests or the application's production HTTP client.
