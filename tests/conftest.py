import json

import pytest

from farstar.config import Settings
from farstar.db import Database
from farstar.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_path=tmp_path / "test.db", secret_key="test-secret-" * 6,
                    admin_password="test-password-long", admin_ids=[999], currency="XTR")


@pytest.fixture
def store(settings):
    db = Database(settings.database_path)
    db.initialize()
    result = Store(db, settings)
    result.save_server({"name": "Germany", "location": "Frankfurt", "url": "https://panel.example/base",
                        "inbound_id": 7, "subscription_url": "https://sub.example/sub", "capacity": 100,
                        "active": True, "trial": True, "credentials": {"token": "private-token"}})
    result.save_plan({"server_id": 1, "name": "Monthly", "days": 30, "volume_gb": 50,
                      "price": 100, "ip_limit": 2, "active": True, "description": "test"})
    result.register(1, "Alice")
    result.accept_terms(1)
    result.adjust_balance(1, 1000, "initial-credit")
    return result


class FakePanel:
    def __init__(self):
        self.clients = {}
        self.adds = 0
        self.updates = 0
        self.fail_after_add = False
        self.fail_after_update = False
        self.update_payloads = []
        self.inbound_rows = [{'enable':True,'protocol':'vless','id':7}]
        self.attachments = []
        self.fail_after_attach = False

    def __call__(self, server, credentials):
        self.server = server
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def inbound(self, inbound_id=None):
        return {"enable": True, "protocol": "vless", "id": 7}

    async def inbounds(self):
        return self.inbound_rows

    async def highest_client_number(self,prefix):
        import re
        pattern=re.compile(re.escape(prefix)+r'(\d+)',re.I)
        return max((int(m[1]) for email in self.clients if (m:=pattern.fullmatch(email))),default=0)

    async def get_client(self, email):
        return json.loads(json.dumps(self.clients.get(email)))

    async def add(self, client, inbound_ids=None):
        from farstar.panel import PanelError
        assert client["email"] not in self.clients
        self.adds += 1
        record = dict(client)
        record["uuid"] = record.pop("id")
        record["id"] = 123
        record["flow"] = "xtls-rprx-vision"
        record["reverse"] = ""
        record["allowedIPs"] = ""
        self.clients[client["email"]] = {"client": record, "inboundIds": list(inbound_ids or [7]), "usedTraffic": 12345}
        if self.fail_after_add:
            self.fail_after_add = False
            raise PanelError("connection lost after remote commit")

    async def attach(self,email,inbound_ids):
        from farstar.panel import PanelError
        self.attachments.append((email,list(inbound_ids)))
        record=self.clients[email]
        record['inboundIds']=sorted(set(record['inboundIds'])|set(inbound_ids))
        if self.fail_after_attach:
            self.fail_after_attach=False
            raise PanelError('connection lost after attachment commit')

    async def update(self, email, client):
        from farstar.panel import PanelError
        self.updates += 1
        self.update_payloads.append(dict(client))
        assert isinstance(client["id"], str), "ClientRecord numeric id must be converted to UUID"
        assert isinstance(client.get("allowedIPs", []), list)
        assert client.get("reverse") is None or isinstance(client["reverse"], dict)
        record = dict(client)
        record["allowedIPs"] = ",".join(record.get("allowedIPs", []))
        record["reverse"] = json.dumps(record["reverse"]) if record.get("reverse") else ""
        record["uuid"] = record.pop("id")
        record["id"] = 123
        self.clients[email]["client"] = record
        if self.fail_after_update:
            self.fail_after_update = False
            raise PanelError("connection lost after update")

    async def links(self, email):
        return ["vless://test@demo.example:443#test"]


@pytest.fixture
def panel():
    return FakePanel()
