import json

import pytest

from farstar.services import GIB, Provisioner, Worker, client_payload
from farstar.store import DomainError


async def purchased(store, panel):
    provisioner = Provisioner(store, panel)
    order = store.create_order(1, 1)
    store.pay_wallet(order["id"], 1)
    await provisioner.process_one()
    return provisioner, store.order(order["id"])


async def test_purchase_readback_and_durable_delivery(store, panel):
    _, order = await purchased(store, panel)
    assert order["status"] == "completed"
    assert panel.adds == 1
    service = store.db.one("SELECT * FROM services")
    assert service["total_bytes"] == 50 * GIB
    assert service["used_bytes"] == 12345
    assert store.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE 'delivery:%'")["n"] == 1


async def test_ambiguous_create_reconciles_without_duplicate(store, panel):
    provisioner = Provisioner(store, panel)
    panel.fail_after_add = True
    order = store.create_order(1, 1)
    store.pay_wallet(order["id"], 1)
    await provisioner.process_one()
    assert store.order(order["id"])["status"] == "paid"
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET next_attempt=0")
    await provisioner.process_one()
    assert store.order(order["id"])["status"] == "completed"
    assert panel.adds == 1
    assert store.user(1)["balance"] == 900


async def test_renewal_retry_is_absolute_and_preserves_protocol_fields(store, panel):
    provisioner, purchase = await purchased(store, panel)
    service = store.db.one("SELECT * FROM services")
    before = panel.clients[service["email"]]["client"].copy()
    renew = store.create_order(1, 1, service_id=purchase["service_id"])
    store.pay_wallet(renew["id"], 1)
    panel.fail_after_update = True
    await provisioner.process_one()
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET next_attempt=0")
    await provisioner.process_one()
    after = panel.clients[service["email"]]["client"]
    assert after["totalGB"] == before["totalGB"] + 50 * GIB
    assert after["expiryTime"] == before["expiryTime"] + 30 * 86400000
    assert after["flow"] == before["flow"]
    assert after["uuid"] == before["uuid"]
    assert panel.update_payloads[0] == panel.update_payloads[1]
    assert store.order(renew["id"])["status"] == "completed"
    assert panel.adds == 1


async def test_one_open_renewal_per_service(store, panel):
    _, purchase = await purchased(store, panel)
    store.create_order(1, 1, service_id=purchase["service_id"])
    with pytest.raises(DomainError):
        store.create_order(1, 1, service_id=purchase["service_id"])


async def test_referral_only_first_completed_purchase(store, panel):
    store.register(2, "Bob", referrer=1)
    store.accept_terms(2)
    store.adjust_balance(2, 1000, "bob-credit")
    p = Provisioner(store, panel)
    for _ in range(2):
        order = store.create_order(2, 1)
        store.pay_wallet(order["id"], 2)
        await p.process_one()
    assert store.user(1)["balance"] == 1010
    assert store.db.one("SELECT COUNT(*) n FROM referral_rewards")["n"] == 1


async def test_refund_requires_remote_disable_and_is_idempotent(store, panel):
    provisioner = Provisioner(store, panel)
    order = store.create_order(1, 1)
    store.pay_wallet(order["id"], 1)
    panel.fail_after_add = True
    await provisioner.process_one()
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET status='failed' WHERE id=?", (order["id"],))
    await provisioner.refund_failed(order["id"])
    assert store.user(1)["balance"] == 1000
    assert not panel.clients[json.loads(store.order(order['id'])["snapshot"])["email"]]["client"]["enable"]
    with pytest.raises(DomainError):
        await provisioner.refund_failed(order["id"])
    assert store.user(1)["balance"] == 1000


async def test_lease_allows_recovery_after_process_crash(store, panel):
    provisioner = Provisioner(store, panel)
    order = store.create_order(1, 1)
    store.pay_wallet(order["id"], 1)
    assert provisioner.claim()
    assert provisioner.claim() is None
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET lease_until=1")
    await provisioner.process_one()
    assert store.order(order["id"])["status"] == "completed"


def test_record_conversion_keeps_new_fields():
    record = {"id": 42, "uuid": "uuid-value", "clientHostRuleIds": [1, 2], "flow": "vision", "createdAt": 1,
              "allowedIPs": "10.0.0.2/32, fd00::2/128", "reverse": '{"tag":"reverse-user"}'}
    payload = client_payload(record)
    assert payload["id"] == "uuid-value" and "uuid" not in payload
    assert payload["clientHostRuleIds"] == [1, 2]
    assert payload["allowedIPs"] == ["10.0.0.2/32", "fd00::2/128"]
    assert payload["reverse"] == {"tag": "reverse-user"}
    assert payload["created_at"] == 1
    assert record["id"] == 42


async def test_expiry_quota_notifications_deduplicated(store, panel):
    _, order = await purchased(store, panel)
    service = store.db.one("SELECT * FROM services")
    remote = panel.clients[service["email"]]
    remote["usedTraffic"] = 49 * GIB
    worker = Worker(store)
    worker.provisioner = Provisioner(store, panel)
    await worker.monitor()
    await worker.monitor()
    assert store.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE 'quota:%'")["n"] == 1
