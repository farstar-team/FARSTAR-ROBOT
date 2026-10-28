import io
import json
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Update
from PIL import Image
from starlette.requests import Request

from farstar.bot import build_dispatcher
from farstar.experience import attention, housekeeping, order_progress, queue_service_alert, sales_report, service_health, storage_status
from farstar.services import Provisioner, Worker
from farstar.store import DomainError
from farstar.uploads import receipt_form
from test_bot import TelegramSession, message
from test_inline_admin import click
from test_miniapp import app_client, auth


async def purchased(store, panel):
    order = store.create_order(1, 1)
    store.pay_wallet(order['id'], 1)
    await Provisioner(store, panel).process_one()
    return store.owned_service(order['id'], 1)


async def test_alias_is_private_and_never_changes_panel_identity(store, panel):
    service = await purchased(store, panel)
    store.register(2, 'Other')
    store.rename_service(service['id'], 1, 'مغازه')
    assert store.owned_service(service['id'], 1)['label'] == 'مغازه'
    for user, name in [(2, 'foreign'), (1, 'x'*41), (1, 'bad\nname'), (1, '\u202ehello')]:
        with pytest.raises(DomainError):
            store.rename_service(service['id'], user, name)
    current = store.owned_service(service['id'], 1)
    assert all(current[k] == service[k] for k in ('email', 'sub_id', 'client_json', 'total_bytes', 'expires_at'))
    assert panel.updates == 0


async def test_alerts_escalate_once_and_do_not_send_lower_tiers_later(store, panel):
    service = await purchased(store, panel)
    service['used_bytes'] = int(service['total_bytes'] * .96)
    queue_service_alert(store, service)
    queue_service_alert(store, service)
    service['used_bytes'] = int(service['total_bytes'] * .81)
    queue_service_alert(store, service)
    notices = store.db.all("SELECT * FROM outbox WHERE dedupe LIKE 'service-notice:%'")
    assert len(notices) == 1 and notices[0]['dedupe'].endswith(':2')
    service['used_bytes'] = service['total_bytes']
    queue_service_alert(store, service)
    assert store.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE 'service-notice:%'")['n'] == 2


async def test_alerts_honor_preferences_bans_trials_and_superseded_renewals(store, panel):
    service = await purchased(store, panel)
    with store.db.transaction() as con:
        con.execute('UPDATE services SET used_bytes=total_bytes WHERE id=?', (service['id'],))
    service = store.owned_service(service['id'], 1)
    store.save_notification_preferences(1, {'expiry_alerts': False, 'quota_alerts': False})
    queue_service_alert(store, service)
    assert not store.db.one("SELECT 1 FROM outbox WHERE dedupe LIKE 'service-notice:%'")
    store.save_notification_preferences(1, {'quota_alerts': True})
    queue_service_alert(store, service)
    # Renew before delivery: a queued stale quota warning must not reach the user.
    with store.db.transaction() as con:
        con.execute('UPDATE services SET total_bytes=total_bytes*2 WHERE id=?', (service['id'],))
        con.execute('UPDATE outbox SET sent_at=1 WHERE dedupe NOT LIKE ?', ('service-notice:%',))
    session = TelegramSession()
    worker = Worker(store, Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session))
    await worker.deliver()
    assert not session.calls
    assert store.db.one("SELECT last_error FROM outbox WHERE dedupe LIKE 'service-notice:%'")['last_error'] == 'superseded'
    for values in [{'unknown': True}, {'quota_alerts': 1}, {}]:
        with pytest.raises(DomainError):
            store.save_notification_preferences(1, values)
    with store.db.transaction() as con:
        con.execute('UPDATE users SET banned=1 WHERE id=1')
    queue_service_alert(store, service)
    assert store.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE 'service-notice:%'")['n'] == 1


@pytest.mark.parametrize('change,status', [({'enabled': 0}, 'disabled'), ({'expires_at': 1}, 'expired'),
                                         ({'used_bytes': 100}, 'exhausted'), ({'used_bytes': 96}, 'low_quota'),
                                         ({'expires_at': 1300}, 'expiring'), ({}, 'healthy')])
def test_diagnostics_explain_real_account_state(change, status):
    service = {'enabled': 1, 'total_bytes': 100, 'used_bytes': 1, 'expires_at': 1000000, 'synced_at': 900} | change
    h = service_health(service, 1000)
    assert h['status'] == status and h['fresh']
    assert 'این بررسی وضعیت حساب' in h['note']


def test_report_uses_tehran_midnight_and_excludes_pending_and_refunds(store):
    timestamp = int(datetime(2026, 10, 28, 1, 0, tzinfo=ZoneInfo('Asia/Tehran')).timestamp())
    midnight = timestamp - 3600
    for i, (at, status, price) in enumerate([(midnight-1, 'completed', 10), (midnight, 'completed', 100),
                                           (midnight+1, 'refunded', 200), (midnight+2, 'pending', 300)]):
        with store.db.transaction() as con:
            con.execute('INSERT INTO orders(id,user_id,server_id,price,currency,status,snapshot,created_at,expires_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (f'report{i}', 1, 1, price, 'XTR', status, json.dumps({'name': 'Plan'}), at, timestamp+3600, at))
    today = sales_report(store.db, 1, timestamp)
    week = sales_report(store.db, 7, timestamp)
    assert today['revenue'] == 100 and today['sales'] == 1 and today['starts_at'] == midnight
    assert week['revenue'] == 110 and len(week['chart']) == 7
    assert week['chart'][-1]['amount'] == 100
    with pytest.raises(ValueError):
        sales_report(store.db, 2)


def test_housekeeping_never_removes_accounting_or_receipt_references(store):
    store.settings.currency = 'IRT'
    store.settings.manual_payments = True
    order = store.create_order(1, 1)
    rid = store.add_receipt(1, order['price'], 'photo', order['id'])
    with store.db.transaction() as con:
        con.execute("INSERT INTO sessions VALUES ('expired','csrf',1)")
        con.execute("INSERT INTO app_sessions VALUES ('expired-app',1,'customer','csrf',1)")
        con.execute("INSERT INTO upload_requests VALUES (1,'completed',?,0)", (rid,))
        con.execute("INSERT INTO upload_requests VALUES (1,'abandoned',NULL,0)")
    before = {t: store.db.all('SELECT * FROM '+t) for t in ('receipts', 'orders', 'ledger', 'outbox')}
    housekeeping(store.db)
    assert all(store.db.all('SELECT * FROM '+t) == rows for t, rows in before.items())
    assert not store.db.all('SELECT * FROM sessions') and not store.db.all('SELECT * FROM app_sessions')
    assert store.db.all('SELECT request_id FROM upload_requests') == [{'request_id': 'completed'}]
    assert storage_status(store.db)['receipt_image_bytes'] == 0
    assert attention(store.db)['counts']['receipts'] == 1


async def test_native_prominent_receipt_button_and_direct_upload_link(store, panel):
    store.settings.currency = 'IRT'
    store.settings.manual_payments = True
    store.settings.card_number = '1111222233334444'
    store.settings.card_holder = 'Test'
    store.settings.mini_app_url = 'https://shop.example/app'
    order = store.create_order(1, 1)
    session = TelegramSession()
    bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session)
    dp = build_dispatcher(store, Provisioner(store, panel), bot)
    await click(dp, bot, 'order:'+order['id'])
    sent = [c for c in session.calls if isinstance(c, SendMessage)][-1]
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    assert next(b for b in buttons if b.callback_data == 'card:'+order['id']).style == 'success'
    await click(dp, bot, 'card:'+order['id'])
    sent = [c for c in session.calls if isinstance(c, SendMessage)][-1]
    primary = sent.reply_markup.inline_keyboard[0][0]
    assert primary.style == 'success' and primary.web_app.url.endswith('#receipt-'+order['id'])


async def test_native_rename_preferences_and_admin_features_are_reachable(store, panel):
    service = await purchased(store, panel)
    session = TelegramSession()
    bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session)
    dp = build_dispatcher(store, Provisioner(store, panel), bot)
    await click(dp, bot, 'rename:'+service['id'])
    await dp.feed_update(bot, Update(update_id=20, message=message('گوشی من')))
    assert store.owned_service(service['id'], 1)['label'] == 'گوشی من'
    await click(dp, bot, 'preferences:quota_alerts')
    assert not store.notification_preferences(1)['quota_alerts']
    for data in ['adm:attention', 'adm:report:7', 'adm:storage']:
        await click(dp, bot, data, 999)
    assert any('فضای سرور' in (getattr(c, 'text', '') or '') for c in session.calls)


def test_mini_reports_and_preferences_require_correct_role_and_csrf(settings):
    app, client = app_client(settings)
    customer = auth(client)
    for route in ['/api/storage', '/api/attention', '/api/reports/sales']:
        assert client.get(route, headers=customer).status_code == 401
    assert client.put('/mini-api/preferences', json={'expiry_alerts': False, 'quota_alerts': True}).status_code == 403
    assert client.put('/mini-api/preferences', headers=customer, json={'expiry_alerts': False, 'quota_alerts': True}).status_code == 200
    assert app.state.store.notification_preferences(1)['expiry_alerts'] is False
    admin = auth(client, 999, 'admin')
    for route in ['/api/storage', '/api/attention', '/api/reports/sales?days=30']:
        assert client.get(route, headers=admin).status_code == 200
    assert client.get('/api/reports/sales?days=999', headers=admin).status_code == 422


def test_large_receipt_never_rolls_to_disk_and_handles_duplicate_fields(settings, monkeypatch):
    settings.currency, settings.manual_payments = 'IRT', True
    settings.card_number = '1111222233334444'
    app, client = app_client(settings)
    fake = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='file',file_unique_id='unique')])))
    app.state.worker.bot = fake
    headers = auth(client)
    store = app.state.store
    store.save_server({'name':'Test','location':'Test','url':'https://panel.example','subscription_url':'https://sub.example',
                       'credentials':{'token':'private'},'inbound_id':7})
    store.save_plan({'name':'Plan','server_id':1,'days':30,'volume_gb':20,'price':100000})
    store.accept_terms(1)
    order = store.create_order(1, 1)
    image = io.BytesIO()
    Image.new('RGB', (40,40), 'white').save(image, 'PNG')
    raw = image.getvalue() + b'\0' * (2 * 1024 * 1024)
    opened = []
    original = tempfile.SpooledTemporaryFile
    def memory_only(*args, **kwargs):
        file = original(*args, **kwargs)
        def fail_rollover():
            pytest.fail('Receipt attempted disk spill')
        file.rollover = fail_rollover
        opened.append(file)
        return file
    monkeypatch.setattr('starlette.formparsers.SpooledTemporaryFile', memory_only)
    response = client.post('/mini-api/receipts', headers=headers, data={'amount':'100000','order_id':order['id'],'request_id':'memory-test'},
                           files={'photo':('photo.png',raw,'image/png')})
    assert response.status_code == 200, response.text
    assert all(file.closed for file in opened) and len(opened) == 1
    assert store.db.one('SELECT file_id FROM receipts')['file_id'] == 'file'
    assert fake.send_photo.call_count == 1
    assert order_progress(store.db, store.order(order['id']))['steps'][1]['done']
    duplicate = client.post('/mini-api/receipts', headers=headers, files=[
        ('photo', ('photo.png', raw, 'image/png')), ('amount', (None, '100000')), ('amount', (None, '100000')),
        ('order_id', (None, order['id'])), ('request_id', (None, 'duplicate-field'))])
    assert duplicate.status_code == 422
    oversized = client.post('/mini-api/receipts', headers=headers,
                            data={'amount':'100000','order_id':order['id'],'request_id':'too-large-photo'},
                            files={'photo':('photo.png', b'x'*(5*1024*1024+1), 'image/png')})
    assert oversized.status_code == 413 and fake.send_photo.call_count == 1
    assert all(file.closed for file in opened)


async def test_chunked_body_limit_closes_partial_upload_without_disk(monkeypatch):
    boundary = 'receipt-boundary'
    first = f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="x.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
    chunks = [first] + [b'x' * (1024 * 1024)] * 7
    async def receive():
        chunk = chunks.pop(0)
        return {'type': 'http.request', 'body': chunk, 'more_body': bool(chunks)}
    request = Request({'type':'http','method':'POST','path':'/mini-api/receipts',
                       'headers':[(b'content-type', f'multipart/form-data; boundary={boundary}'.encode())]}, receive)
    files = []
    original = tempfile.SpooledTemporaryFile
    def track(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file
    monkeypatch.setattr('starlette.formparsers.SpooledTemporaryFile', track)
    with pytest.raises(Exception) as exc:
        async with receipt_form(request):
            pytest.fail('Oversized chunked request accepted')
    assert getattr(exc.value, 'status_code', None) == 413
    assert files and all(f.closed and not f._rolled for f in files)
