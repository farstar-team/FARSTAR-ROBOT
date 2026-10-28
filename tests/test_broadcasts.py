import hashlib
import json

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Update

from farstar.bot import build_dispatcher
from farstar.broadcasts import audience, import_contacts, status
from farstar.services import Provisioner, Worker
from farstar.store import DomainError
from test_bot import TelegramSession, message
from test_inline_admin import click
from test_miniapp import app_client, auth

SOURCE = 'a'*64


def test_legacy_contacts_deduplicate_without_creating_accounts_or_wallets(store):
    before = {t:store.db.all('SELECT * FROM '+t) for t in ('users','ledger','orders','services')}
    imported = import_contacts(store.db,[1,2,2,3],SOURCE)
    assert imported == {'unique':3,'added':3,'recipients':3}
    assert import_contacts(store.db,[1,2,3],SOURCE)['added']==0
    assert all(store.db.all('SELECT * FROM '+t)==rows for t,rows in before.items())
    assert store.broadcast('بازگشت', '1'*16)==3
    assert {r['user_id'] for r in store.db.all("SELECT user_id FROM outbox WHERE dedupe LIKE 'broadcast:%'")}=={1,2,3}


def test_known_admin_bans_are_respected_for_imported_contacts(store):
    store.register(2,'Blocked')
    with store.db.transaction() as con:
        con.execute('UPDATE users SET banned=1 WHERE id=2')
    import_contacts(store.db,[1,2,3],SOURCE)
    assert audience(store.db)==2
    store.broadcast('hello')
    assert not store.db.one("SELECT 1 FROM outbox WHERE user_id=2 AND dedupe LIKE 'broadcast:%'")


@pytest.mark.parametrize('ids', [[],[True],[0],[-100],[2**52],['100'],[1,2.0]])
def test_bad_contact_ids_cannot_partially_import(store,ids):
    with pytest.raises(DomainError):
        import_contacts(store.db,ids,SOURCE)
    assert not store.db.all('SELECT * FROM broadcast_contacts')


def test_broadcast_confirm_retry_is_idempotent_and_rejects_reused_key_for_other_text(store):
    import_contacts(store.db,[1,2,3],SOURCE)
    key='2'*16
    assert store.broadcast('hello',key)==3
    assert store.broadcast('hello',key)==3
    assert status(store.db,key)=={'id':key,'created_at':status(store.db,key)['created_at'],'total':3,'sent':0,'pending':3,'failed':0}
    with pytest.raises(DomainError):
        store.broadcast('different',key)
    assert store.db.one("SELECT COUNT(*) n FROM outbox WHERE dedupe LIKE 'broadcast:%'")['n']==3
    with store.db.transaction() as con:
        con.execute("UPDATE outbox SET sent_at=1 WHERE user_id=1 AND dedupe LIKE 'broadcast:%'")
        con.execute("UPDATE outbox SET attempts=10,last_error='telegram_rejected' WHERE user_id=2 AND dedupe LIKE 'broadcast:%'")
    r=status(store.db)
    assert (r['sent'],r['pending'],r['failed'])==(1,1,1)


async def test_customer_delivery_precedes_large_broadcast_and_legacy_recipients_need_no_accounts(store):
    import_contacts(store.db,list(range(10,40)),SOURCE)
    store.broadcast('legacy campaign','3'*16)
    with store.db.transaction() as con:
        con.execute("INSERT INTO outbox(user_id,body,dedupe) VALUES (1,'payment accepted','receipt-result:test')")
    session=TelegramSession()
    worker=Worker(store,Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',session=session))
    await worker.deliver()
    calls=[c for c in session.calls if isinstance(c,SendMessage)]
    assert calls[0].text=='payment accepted'
    assert any(int(c.chat_id)>=10 for c in calls)
    assert len(store.db.all('SELECT * FROM users'))==1
    assert status(store.db,'3'*16)['sent']==24


async def test_native_preview_is_bound_to_its_text_and_has_progress_report(store,panel):
    import_contacts(store.db,[1,2,3],SOURCE)
    session=TelegramSession()
    bot=Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',session=session)
    dp=build_dispatcher(store,Provisioner(store,panel),bot)
    await click(dp,bot,'adm:broadcast',999)
    await dp.feed_update(bot,Update(update_id=80,message=message('فرستار برگشته',999)))
    data=json.loads(store.user(999)['state_data'])
    assert data['text']=='فرستار برگشته'
    await click(dp,bot,'adm:broadcastconfirm:'+'0'*16,999)
    assert not store.db.one("SELECT 1 FROM outbox WHERE dedupe LIKE 'broadcast:%'")
    await click(dp,bot,'adm:broadcastconfirm:'+data['campaign_id'],999)
    await click(dp,bot,'adm:broadcastconfirm:'+data['campaign_id'],999)
    assert status(store.db,data['campaign_id'])['total']==4
    await click(dp,bot,'adm:broadcaststatus:'+data['campaign_id'],999)
    assert any('گزارش پیام همگانی' in (getattr(c,'text','') or '') for c in session.calls)


def test_only_admin_can_read_audience_and_progress(settings):
    app,client=app_client(settings)
    customer=auth(client)
    assert client.get('/api/broadcast-audience',headers=customer).status_code==401
    assert client.get('/api/broadcast-status',headers=customer).status_code==401
    admin=auth(client,999,'admin')
    assert client.get('/api/broadcast-audience',headers=admin).status_code==200
    assert client.get('/api/broadcast-status',headers=admin).status_code==400
    body=hashlib.sha256(b'safe sample').hexdigest()
    app.state.store.broadcast(body,'4'*16)
    assert client.get('/api/broadcast-status',headers=admin).json()['total']==2
