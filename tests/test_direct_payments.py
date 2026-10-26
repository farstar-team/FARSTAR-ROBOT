import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from aiogram import Bot
from aiogram.methods import SendPhoto
from aiogram.types import PhotoSize, Update
from PIL import Image

from farstar.bot import build_dispatcher
from farstar.qr import subscription_card
from farstar.services import Provisioner, Worker, subscription
from farstar.store import DomainError, now
from test_bot import TelegramSession, message
from test_inline_admin import click


def card_order(store):
    store.settings.currency='IRT'
    store.settings.manual_payments=True
    store.settings.card_number='1111222233334444'
    store.settings.card_holder='Test Shop'
    return store.create_order(1,1)


def test_receipt_approval_never_touches_wallet_and_concurrent_clicks_pay_once(store):
    order=card_order(store)
    rid=store.add_receipt(1,order['price'],'one-photo',order['id'])
    assert store.order(order['id'])['status']=='awaiting_review'
    def approve(_):
        try:
            store.review_receipt(rid,True,999)
            return True
        except DomainError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(approve,range(8)))==1
    assert store.user(1)['balance']==1000
    assert store.db.one('SELECT COUNT(*) n FROM ledger')['n']==1
    assert store.order(order['id'])['status']=='paid'
    assert store.order(order['id'])['charge_id']=='receipt:'+rid


def test_review_hold_blocks_expiry_cancel_wallet_and_duplicate_receipt(store):
    order=card_order(store)
    store.add_receipt(1,order['price'],'one-photo',order['id'],file_unique_id='same-photo')
    with store.db.transaction() as con:
        con.execute('UPDATE orders SET expires_at=1 WHERE id=?',(order['id'],))
    store.create_order(1,1)
    assert store.order(order['id'])['status']=='awaiting_review'
    with pytest.raises(DomainError):
        store.cancel_order(order['id'],1)
    with pytest.raises(DomainError):
        store.pay_wallet(order['id'],1)
    with pytest.raises(DomainError):
        store.add_receipt(1,order['price'],'another-photo',order['id'])
    assert store.user(1)['balance']==1000


def test_rejection_reopens_same_order_and_requires_a_new_receipt(store):
    order=card_order(store)
    rid=store.add_receipt(1,order['price'],'one-photo',order['id'],file_unique_id='same-photo')
    store.review_receipt(rid,False,999)
    assert store.order(order['id'])['status']=='pending'
    assert store.order(order['id'])['expires_at']>now()
    with pytest.raises(DomainError):
        store.add_receipt(1,order['price'],'reuploaded-photo',order['id'],file_unique_id='same-photo')
    second=store.add_receipt(1,order['price'],'new-photo',order['id'],file_unique_id='new-photo')
    store.review_receipt(second,True,999)
    assert store.order(order['id'])['status']=='paid' and store.user(1)['balance']==1000


def test_receipts_require_owner_exact_price_and_cannot_be_reused_between_orders(store):
    order=card_order(store)
    with pytest.raises(DomainError):
        store.add_receipt(2,order['price'],'wrong-user',order['id'])
    with pytest.raises(DomainError):
        store.add_receipt(1,order['price']-1,'wrong-price',order['id'])
    store.add_receipt(1,order['price'],'photo',order['id'],image_hash='same-image')
    other=store.create_order(1,1)
    with pytest.raises(DomainError):
        store.add_receipt(1,other['price'],'another-file-id',other['id'],image_hash='same-image')
    assert store.order(other['id'])['status']=='pending'


async def test_direct_receipt_first_admin_notification_has_photo_buttons_and_approval_delivers_qr(store,panel):
    order=card_order(store)
    session=TelegramSession()
    bot=Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',session=session)
    worker=Worker(store,bot)
    worker.provisioner=Provisioner(store,panel)
    rid=store.add_receipt(1,order['price'],'receipt-photo',order['id'])
    await worker.deliver()
    photos=[c for c in session.calls if isinstance(c,SendPhoto) and c.chat_id==999]
    assert len(photos)==1 and photos[0].photo=='receipt-photo'
    assert {b.callback_data for row in photos[0].reply_markup.inline_keyboard for b in row}=={
        'adm:receiptapprove:'+rid,'adm:receiptreject:'+rid}
    assert order['id'] in photos[0].caption
    dp=build_dispatcher(store,worker.provisioner,bot)
    await click(dp,bot,'adm:receiptapprove:'+rid,999)
    await worker.provisioner.process_one()
    await worker.deliver()
    sent=[c for c in session.calls if isinstance(c,SendPhoto) and c.chat_id==1]
    assert len(sent)==1 and sent[0].photo.data.startswith(b'\x89PNG')
    service=store.db.one('SELECT * FROM services')
    assert subscription(store.server(1),service['sub_id']) in sent[0].caption
    assert service['email'] in sent[0].caption and store.user(1)['balance']==1000
    await click(dp,bot,'adm:receiptapprove:'+rid,999,2)
    await worker.provisioner.process_one()
    await worker.deliver()
    assert panel.adds==1
    assert len([c for c in session.calls if isinstance(c,SendPhoto) and c.chat_id==1])==1


async def test_native_card_checkout_does_not_ask_for_wallet_topup(store,panel):
    order=card_order(store)
    session=TelegramSession()
    bot=Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',session=session)
    dp=build_dispatcher(store,Provisioner(store,panel),bot)
    await click(dp,bot,'card:'+order['id'])
    assert store.user(1)['state']=='receipt_photo'
    assert json.loads(store.user(1)['state_data'])['order_id']==order['id']
    photo=message().model_copy(update={'photo':[PhotoSize(file_id='receipt',file_unique_id='unique-receipt',width=300,height=400)]})
    await dp.feed_update(bot,Update(update_id=2,message=photo))
    assert store.order(order['id'])['status']=='awaiting_review'
    assert store.user(1)['balance']==1000


async def test_non_admin_cannot_use_receipt_approval_button(store,panel):
    order=card_order(store)
    rid=store.add_receipt(1,order['price'],'photo',order['id'])
    bot=Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',session=TelegramSession())
    dp=build_dispatcher(store,Provisioner(store,panel),bot)
    await click(dp,bot,'adm:receiptapprove:'+rid,1)
    assert store.order(order['id'])['status']=='awaiting_review'


@pytest.mark.parametrize('link',['https://sub.example.com:2096/private-path/unique-personal-subscription',
                                'https://sub.example.com:2096/'+'path'*100+'/personal-subscription'])
def test_branded_personal_qr_roundtrips_after_jpeg_compression(link):
    import zxingcpp
    service={'email':'FARSTAR_406','total_bytes':20*1024**3,'expires_at':now()+30*86400}
    png=subscription_card(link,service)
    with Image.open(io.BytesIO(png)) as image:
        assert image.size==(800,1040)
        assert zxingcpp.read_barcode(image).text==link
        jpeg=io.BytesIO()
        image.save(jpeg,'JPEG',quality=70)
        with Image.open(io.BytesIO(jpeg.getvalue())) as compressed:
            assert zxingcpp.read_barcode(compressed).text==link


async def test_failed_card_order_requires_manual_bank_refund_without_wallet_credit(store,panel):
    order=card_order(store)
    rid=store.add_receipt(1,order['price'],'photo',order['id'])
    store.review_receipt(rid,True,999)
    p=Provisioner(store,panel)
    await p.process_one()
    with store.db.transaction() as con:
        con.execute("UPDATE orders SET status='failed' WHERE id=?",(order['id'],))
    await p.refund_failed(order['id'])
    assert store.order(order['id'])['status']=='refund_pending'
    assert store.user(1)['balance']==1000
    assert store.db.one("SELECT COUNT(*) n FROM ledger WHERE kind='refund'")['n']==0


def test_fresh_demo_can_seed_after_safe_schema_extensions(settings):
    from farstar.db import Database
    from farstar.store import Store
    from farstar.demo import seed
    db=Database(settings.database_path)
    db.initialize()
    store=Store(db,settings)
    seed(store)
    assert len(db.all('SELECT * FROM servers'))==4
    assert len(db.all('SELECT * FROM services'))>0
