import json

import httpx
import pytest
from fastapi.testclient import TestClient

from farstar.api import create_app
from farstar.panel import Panel, PanelError


def client_for(settings):
    app = create_app(settings, run_background=False)
    client = TestClient(app)
    return app, client


def login(client):
    r = client.post('/api/auth/login', json={"username": "admin", "password": "test-password-long"})
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf"]}


def test_auth_csrf_logout_and_headers(settings):
    _, client = client_for(settings)
    assert client.get('/api/overview').status_code == 401
    headers = login(client)
    r = client.get('/api/overview')
    assert r.status_code == 200
    assert r.headers['X-Frame-Options'] == 'DENY'
    assert client.post('/api/broadcast', json={"text": "hi"}).status_code == 403
    assert client.post('/api/auth/logout', headers=headers).status_code == 200
    assert client.get('/api/overview').status_code == 401


def test_create_server_plan_and_credentials_not_disclosed(settings):
    _, client = client_for(settings)
    headers = login(client)
    server = {"name": "Germany", "location": "DE", "url": "https://example.com/path", "inbound_id": 1,
              "subscription_url": "https://sub.example.com/sub", "credentials": {"token": "very-secret"}}
    assert client.post('/api/servers', json=server, headers=headers).status_code == 200
    listed = client.get('/api/servers').json()
    assert "very-secret" not in json.dumps(listed) and 'credentials' not in listed[0]
    plan = {"name": "One month", "server_id": 1, "days": 30, "volume_gb": 50, "price": 100}
    assert client.post('/api/plans', json=plan, headers=headers).status_code == 200
    assert client.get('/api/plans').json()[0]['volume_gb'] == 50
    server['url'] = 'file:///etc/passwd'
    assert client.post('/api/servers', json=server, headers=headers).status_code == 422


def test_export_escapes_formula_and_backup_is_valid(settings):
    app, client = client_for(settings)
    app.state.store.register(1, '=CMD()')
    login(client)
    exported = client.get('/api/export/users')
    assert "'=CMD()" in exported.text
    backup = client.get('/api/backup')
    assert backup.content.startswith(b'SQLite format 3')


def test_login_bruteforce_throttle(settings):
    _, client = client_for(settings)
    for _ in range(10):
        assert client.post('/api/auth/login', json={"username": "bad", "password": "bad"}).status_code == 401
    assert client.post('/api/auth/login', json={"username": "admin", "password": "test-password-long"}).status_code == 429


async def test_latest_api_paths_payload_and_base_path(store):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers['Authorization'] == 'Bearer private-token'
        return httpx.Response(200, json={"success": True, "obj": {"client": {"email": "alice"}, "inboundIds": [7]}})

    async with Panel(store.server(1), {"token": "private-token"}, transport=httpx.MockTransport(handle)) as panel:
        await panel.get_client("alice+tag")
        await panel.add({"email": "alice"})
        await panel.update("alice", {"email": "alice", "enable": False})
    assert calls[0].url.path == '/base/panel/api/clients/get/alice+tag'
    assert calls[1].url.path == '/base/panel/api/clients/add'
    assert json.loads(calls[1].content)['inboundIds'] == [7]
    assert calls[2].url.path == '/base/panel/api/clients/update/alice'


async def test_panel_missing_distinguished_from_auth_and_generic_errors(store):
    responses = [httpx.Response(200, json={"success": False, "msg": "record not found", "obj": None}),
                 httpx.Response(401), httpx.Response(200, json={"success": False, "msg": "database locked", "obj": None})]
    def handle(request):
        return responses.pop(0)
    async with Panel(store.server(1), {"token": "x"}, transport=httpx.MockTransport(handle)) as panel:
        assert await panel.get_client('absent') is None
        with pytest.raises(PanelError):
            await panel.get_client('absent')
        with pytest.raises(PanelError):
            await panel.get_client('absent')


async def test_panel_password_login(store):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "obj": {}}, headers={'set-cookie': 'session=abc; Path=/'})
    async with Panel(store.server(1), {"username": "admin", "password": "secret"}, transport=httpx.MockTransport(handle)) as panel:
        await panel.inbound()
    assert requests[0].url.path == '/base/login'
    assert 'session=abc' in requests[1].headers['cookie']


def test_payment_card_configuration_is_persistent_and_protected(settings):
    app, client = client_for(settings)
    payload = {"card_number": "۶۰۳۷ ۹۹۷۵ ۱۲۳۴ ۵۶۷۸", "card_holder": "حساب فروشگاه"}
    assert client.put('/api/payment-settings', json=payload).status_code == 401
    headers = login(client)
    assert client.put('/api/payment-settings', json=payload, headers=headers).status_code == 200
    assert client.get('/api/payment-settings').json()['card_number'] == '6037997512345678'
    from farstar.store import Store
    fresh = settings.model_copy(update={"card_number": "", "card_holder": ""})
    Store(app.state.store.db, fresh)
    assert fresh.card_number == '6037997512345678'
    assert fresh.card_holder == 'حساب فروشگاه'
    payload['card_number'] = '123'
    assert client.put('/api/payment-settings', json=payload, headers=headers).status_code == 422
