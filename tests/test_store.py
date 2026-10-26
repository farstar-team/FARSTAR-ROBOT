from concurrent.futures import ThreadPoolExecutor

import pytest

from farstar.store import DomainError, now


def test_wallet_payment_is_atomic_and_idempotent(store):
    order = store.create_order(1, 1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.pay_wallet(order["id"], 1), range(8)))
    assert store.user(1)["balance"] == 900
    assert store.db.one("SELECT COUNT(*) n FROM ledger WHERE kind='purchase'")["n"] == 1
    assert store.order(order["id"])["status"] == "paid"


def test_insufficient_funds_rolls_back(store):
    store.adjust_balance(1, -950, "withdraw")
    order = store.create_order(1, 1)
    with pytest.raises(DomainError):
        store.pay_wallet(order["id"], 1)
    assert store.user(1)["balance"] == 50
    assert store.order(order["id"])["status"] == "pending"


def test_ownership_and_expiry(store):
    order = store.create_order(1, 1)
    with pytest.raises(DomainError):
        store.pay_wallet(order["id"], 2)
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET expires_at=1 WHERE id=?", (order["id"],))
    with pytest.raises(DomainError):
        store.pay_wallet(order["id"], 1)
    assert store.user(1)["balance"] == 1000


def test_trial_and_terms(store):
    trial = store.create_order(1, trial=True)
    assert trial["status"] == "paid" and trial["price"] == 0
    with pytest.raises(DomainError):
        store.create_order(1, trial=True)
    store.register(2, "Bob")
    with pytest.raises(DomainError):
        store.create_order(2, 1)


def test_coupon_capacity_reserved_atomically(store):
    store.register(2, "Bob")
    store.accept_terms(2)
    with store.db.transaction() as con:
        con.execute("INSERT INTO coupons VALUES ('ONE',25,1,?,1)", (now() + 1000,))
    first = store.create_order(1, 1, "one")
    assert first["price"] == 75
    with pytest.raises(DomainError):
        store.create_order(2, 1, "ONE")
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET expires_at=1 WHERE id=?", (first["id"],))
    assert store.create_order(2, 1, "ONE")["price"] == 75


def test_gift_cannot_be_claimed_twice(store):
    with store.db.transaction() as con:
        con.execute("INSERT INTO gifts VALUES ('GIFT',30,1,?)", (now() + 1000,))
    assert store.redeem(1, "gift") == 30
    with pytest.raises(DomainError):
        store.redeem(1, "GIFT")
    assert store.user(1)["balance"] == 1030


def test_stars_validation_and_duplicate_charges(store):
    order = store.create_order(1, 1)
    with pytest.raises(DomainError):
        store.precheckout(order["id"], 1, 99, "XTR")
    store.precheckout(order["id"], 1, 100, "XTR")
    with pytest.raises(DomainError):
        store.precheckout(order["id"], 1, 100, "XTR")
    store.settle_stars(order["id"], 1, 100, "XTR", "charge1")
    store.settle_stars(order["id"], 1, 100, "XTR", "charge1")
    store.settle_stars(order["id"], 1, 100, "XTR", "charge2")
    assert store.db.one("SELECT COUNT(*) n FROM payments")["n"] == 2
    assert store.db.one("SELECT status FROM payments WHERE charge_id='charge2'")["status"] == "refund_pending"
    assert store.order(order["id"])["charge_id"] == "charge1"


def test_successful_payment_after_invoice_expiry_is_not_lost(store):
    order = store.create_order(1, 1)
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET status='expired' WHERE id=?", (order["id"],))
    store.settle_stars(order["id"], 1, 100, "XTR", "late-charge")
    assert store.order(order["id"])["status"] == "paid"


def test_receipt_review_pays_order_once_without_wallet_credit(store):
    store.settings.manual_payments = True
    store.settings.currency = "IRT"
    order=store.create_order(1,1)
    receipt = store.add_receipt(1, order['price'], "file-one",order['id'])
    store.review_receipt(receipt, True, "admin")
    with pytest.raises(DomainError):
        store.review_receipt(receipt, True, "admin")
    assert store.user(1)["balance"] == 1000
    assert store.order(order['id'])['status']=='paid'
    assert store.order(order['id'])['payment_method']=='card'


def test_ban_prevents_checkout(store):
    with store.db.transaction() as con:
        con.execute("UPDATE users SET banned=1 WHERE id=1")
    with pytest.raises(DomainError):
        store.create_order(1, 1)


def test_server_credentials_encrypted_and_identity_changes_protected(store):
    server = store.server(1)
    assert "private-token" not in server["credentials"]
    assert store.vault.open(server["credentials"])["token"] == "private-token"
    store.create_order(1, 1)
    with pytest.raises(DomainError):
        store.save_server({"inbound_id": 8}, 1)


def test_ticket_ownership_and_outbox(store):
    ticket = store.ticket(1, "Please help")
    with pytest.raises(DomainError):
        store.ticket(2, "steal ticket", ticket)
    store.ticket(999, "Fixed", ticket, admin=True)
    assert store.db.one("SELECT status FROM tickets WHERE id=?", (ticket,))["status"] == "answered"
    assert store.db.one("SELECT COUNT(*) n FROM outbox WHERE user_id=1")["n"] == 1


def test_cancel_invoice_frees_pending_limit_and_cannot_be_paid(store):
    orders = [store.create_order(1, 1) for _ in range(3)]
    with pytest.raises(DomainError):
        store.create_order(1, 1)
    store.cancel_order(orders[0]["id"], 1)
    with pytest.raises(DomainError):
        store.pay_wallet(orders[0]["id"], 1)
    assert store.create_order(1, 1)["status"] == "pending"


def test_currency_cannot_change_after_transactions(store):
    from farstar.store import Store
    store.settings.currency = "IRT"
    with pytest.raises(ValueError, match="CURRENCY"):
        Store(store.db, store.settings)
