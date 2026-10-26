import hmac
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from farstar.api import create_app
from farstar.happ import DEVICES
from farstar.security import validate_telegram_init_data
from farstar.store import now

TOKEN = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'


def signed(user_id=1, timestamp=None, **extra):
    data={'auth_date':str(now() if timestamp is None else timestamp),
          'user':json.dumps({'id':user_id,'first_name':'Test','username':'test_user'},separators=(',',':')),
          'query_id':'test-query',**extra}
    # Reference algorithm from Telegram's Mini Apps documentation.
    key=hmac.digest(b'WebAppData',TOKEN.encode(),'sha256')
    data['hash']=hmac.digest(key,'\n'.join(k+'='+v for k,v in sorted(data.items())).encode(),'sha256').hex()
    return urlencode(data)


def app_client(settings):
    settings.bot_token=TOKEN
    app=create_app(settings,run_background=False)
    return app,TestClient(app)


def auth(client,user_id=1,role='customer'):
    result=client.post('/mini-api/auth',json={'init_data':signed(user_id),'role':role})
    assert result.status_code==200,result.text
    return {'X-CSRF-Token':result.json()['csrf'],'X-Mini-Session':result.json()['session_token']}


def test_signature_reference_and_tamper():
    assert validate_telegram_init_data(signed(),TOKEN)['id']==1
    with pytest.raises(ValueError):
        validate_telegram_init_data(signed().replace('Test','Other'),TOKEN)
    with pytest.raises(ValueError):
        validate_telegram_init_data(signed()+'&auth_date=1',TOKEN)
    with pytest.raises(ValueError):
        validate_telegram_init_data(signed(timestamp=now()-601),TOKEN)
    with pytest.raises(ValueError):
        validate_telegram_init_data(signed(timestamp=now()+60),TOKEN)


def test_hmac_includes_telegram_signature_field():
    assert validate_telegram_init_data(signed(signature='optional-signature'),TOKEN)['id']==1


def test_customer_cannot_claim_admin_role_or_access_admin_api(settings):
    _,client=app_client(settings)
    result=client.post('/mini-api/auth',json={'init_data':signed(1),'role':'admin'})
    assert result.status_code==403
    headers=auth(client)
    assert client.get('/api/overview',headers=headers).status_code==401
    assert client.get('/mini-api/session?role=admin',headers=headers).status_code==401
    assert client.post('/mini-api/demo-login',json={'role':'admin'}).status_code==404


def test_admin_mini_session_authorizes_existing_api_with_csrf(settings):
    _,client=app_client(settings)
    headers=auth(client,999,'admin')
    assert client.get('/api/overview',headers=headers).status_code==200
    assert client.post('/api/broadcast',json={'text':'test'}).status_code==403
    assert client.post('/api/broadcast',json={'text':'test'},headers=headers).status_code==200
    assert client.post('/api/auth/logout',headers=headers).status_code==200
    assert client.get('/api/overview',headers=headers).status_code==401


def test_shop_settings_admin_only_and_survive_restart(settings):
    from farstar.store import Store
    app,client=app_client(settings)
    customer=auth(client)
    values={'trial_enabled':False,'trial_gb':2,'trial_hours':12,'referral_percent':5}
    assert client.put('/api/shop-settings',json=values,headers=customer).status_code==401
    admin=auth(client,999,'admin')
    assert client.put('/api/shop-settings',json=values,headers=admin).status_code==200
    assert client.get('/api/shop-settings',headers=admin).json()==values
    settings.trial_enabled=True
    settings.referral_percent=10
    Store(app.state.store.db,settings)
    assert settings.trial_enabled is False and settings.referral_percent==5
    invalid={**values,'referral_percent':101}
    assert client.put('/api/shop-settings',json=invalid,headers=admin).status_code==422


def test_mini_app_url_requires_https(settings):
    from farstar.config import Settings
    values=settings.model_dump()
    with pytest.raises(ValueError):
        Settings(**{**values,'mini_app_url':'http://example.com/app'})
    assert Settings(**{**values,'mini_app_url':'https://example.com/app'}).mini_app_url.endswith('/app')


def test_in_memory_token_works_when_third_party_cookies_are_blocked(settings):
    _,client=app_client(settings)
    headers=auth(client)
    client.cookies.clear()
    assert client.get('/mini-api/me',headers=headers).status_code==200
    assert client.post('/mini-api/terms/accept',headers=headers).status_code==200
    invalid={**headers,'X-CSRF-Token':'wrong'}
    assert client.post('/mini-api/terms/accept',headers=invalid).status_code==403
    cross={**headers,'Origin':'https://attacker.example'}
    assert client.post('/mini-api/terms/accept',headers=cross).status_code==403


def test_ban_revokes_customer_session(settings):
    app,client=app_client(settings)
    headers=auth(client)
    with app.state.store.db.transaction() as con:
        con.execute('UPDATE users SET banned=1 WHERE id=1')
    assert client.get('/mini-api/me',headers=headers).status_code==401


def test_customer_order_ownership_and_wallet_flow(settings):
    app,client=app_client(settings)
    store=app.state.store
    store.save_server({'name':'Germany','location':'DE','url':'https://panel.example','inbound_id':1,
                       'subscription_url':'https://sub.example/sub','credentials':{'token':'secret'}})
    store.save_plan({'server_id':1,'name':'Monthly','days':30,'volume_gb':20,'price':100,'ip_limit':2,'active':True,'description':''})
    owner=auth(client,1)
    assert client.post('/mini-api/orders',json={'plan_id':1},headers=owner).status_code==400
    assert client.post('/mini-api/terms/accept',headers=owner).status_code==200
    store.adjust_balance(1,200,'initial-mock-credit')
    result=client.post('/mini-api/orders',json={'plan_id':1},headers=owner)
    assert result.status_code==200
    order_id=result.json()['id']
    other=auth(client,2)
    assert client.post('/mini-api/orders/'+order_id+'/wallet',headers=other).status_code==400
    assert client.post('/mini-api/orders/'+order_id+'/wallet',headers=owner).status_code==200
    assert client.post('/mini-api/orders/'+order_id+'/wallet',headers=owner).status_code==200
    assert store.user(1)['balance']==100
    assert 'snapshot' not in result.json() and 'sub_id' not in result.text


def test_customer_cannot_read_another_users_tickets(settings):
    app,client=app_client(settings)
    one=auth(client,1)
    result=client.post('/mini-api/tickets',json={'text':'private support message'},headers=one)
    assert result.status_code==200
    two=auth(client,2)
    assert client.get('/mini-api/tickets/'+str(result.json()['id']),headers=two).status_code==400
    assert 'private support message' not in client.get('/mini-api/tickets',headers=two).text


def test_receipt_upload_is_idempotent_and_never_credits_automatically(settings):
    settings.currency='IRT'
    settings.manual_payments=True
    settings.card_number='1111222233334444'
    settings.card_holder='Test shop'
    app,client=app_client(settings)
    fake=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id='test-photo')])) )
    app.state.worker.bot=fake
    headers=auth(client)
    server={'name':'Server','location':'test','url':'https://panel.example/base','inbound_id':7,
            'subscription_url':'https://sub.example/sub','credentials':{'token':'test-token'}}
    app.state.store.save_server(server)
    plan=app.state.store.save_plan({'name':'Plan','server_id':1,'days':30,'volume_gb':20,'price':100000})
    app.state.store.accept_terms(1)
    order=app.state.store.create_order(1,plan)
    image=io.BytesIO()
    Image.new('RGB',(40,40),'white').save(image,'PNG')
    def send(amount='100000'):
        return client.post('/mini-api/receipts',headers=headers,data={'amount':amount,'request_id':'same-request-id','order_id':order['id']},
                           files={'photo':('test.png',image.getvalue(),'image/png')})
    first=send()
    assert first.status_code==200,first.text
    assert send().json()['id']==first.json()['id']
    assert fake.send_photo.call_count==1
    assert app.state.store.user(1)['balance']==0
    assert send('200000').status_code==400
    assert app.state.store.db.one('SELECT COUNT(*) n FROM receipts')['n']==1
    assert app.state.store.order(order['id'])['status']=='awaiting_review'


def test_invalid_receipt_file_is_rejected_without_sending(settings):
    settings.currency='IRT'
    settings.manual_payments=True
    settings.card_number='1111222233334444'
    app,client=app_client(settings)
    fake=SimpleNamespace(send_photo=AsyncMock())
    app.state.worker.bot=fake
    headers=auth(client)
    result=client.post('/mini-api/receipts',headers=headers,data={'amount':'100000','request_id':'bad-image-request','order_id':'unknown-order'},
                       files={'photo':('invalid.png',b'not an image','image/png')})
    assert result.status_code==400
    assert not fake.send_photo.called


def test_mini_html_allows_only_telegram_embedding_and_sdk(settings):
    _,client=app_client(settings)
    result=client.get('/app')
    assert result.status_code==200
    assert 'https://telegram.org' in result.headers['content-security-policy']
    assert 'https://web.telegram.org' in result.headers['content-security-policy']
    assert 'x-frame-options' not in result.headers
    assert client.get('/').headers['x-frame-options']=='DENY'
    assert client.get('/admin-app').status_code==200


def test_all_device_guides_have_real_animated_assets():
    from pathlib import Path
    root=Path(__file__).resolve().parent.parent/'farstar/static'
    assert len(DEVICES)==7
    for key,device in DEVICES.items():
        with Image.open(root/f'happ-{key}.gif') as gif:
            assert gif.n_frames>=4
        assert len(device['steps'])==4
        assert all(url.startswith('https://') for _,url in device['links'])
