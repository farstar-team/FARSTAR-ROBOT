import json
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update, User

from farstar.bot import build_dispatcher
from farstar.services import Provisioner


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto)):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id), type="private"), text=getattr(method, 'text', None))
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b"test"


def actor(user_id=1):
    return User(id=user_id, is_bot=False, first_name="Alice")


def message(text="", user_id=1, payment=None):
    return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type="private"),
                   from_user=actor(user_id), text=text or None, successful_payment=payment)


async def test_actual_dispatcher_purchase_and_qr(store, panel):
    session = TelegramSession()
    bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session,
              default=DefaultBotProperties(parse_mode='HTML'))
    p = Provisioner(store, panel)
    dp = build_dispatcher(store, p, bot)
    await dp.feed_update(bot, Update(update_id=1, message=message('/start')))
    await dp.feed_update(bot, Update(update_id=2, message=message('🛍 خرید سرویس')))
    assert any('پلن‌های فروشگاه' in (getattr(c, 'text', '') or '') for c in session.calls)
    query = CallbackQuery(id='buy', from_user=actor(), chat_instance='x', data='buy:1:-', message=message())
    await dp.feed_update(bot, Update(update_id=3, callback_query=query))
    order = store.db.one("SELECT * FROM orders")
    query = query.model_copy(update={"id": "wallet", "data": "wallet:" + order['id']})
    await dp.feed_update(bot, Update(update_id=4, callback_query=query))
    await p.process_one()
    assert store.order(order['id'])['status'] == 'completed'
    query = query.model_copy(update={"id": "qr", "data": "qr:" + order['id']})
    await dp.feed_update(bot, Update(update_id=5, callback_query=query))
    assert any(isinstance(c, SendPhoto) for c in session.calls)


async def test_successful_payment_processed_even_if_user_is_banned(store, panel):
    session = TelegramSession()
    bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session)
    dp = build_dispatcher(store, Provisioner(store, panel), bot)
    order = store.create_order(1, 1)
    pre = PreCheckoutQuery(id='q', from_user=actor(), currency='XTR', total_amount=100,
                          invoice_payload='order:' + order['id'])
    await dp.feed_update(bot, Update(update_id=1, pre_checkout_query=pre))
    assert store.order(order['id'])['status'] == 'checkout'
    with store.db.transaction() as con:
        con.execute("UPDATE users SET banned=1 WHERE id=1")
    payment = SuccessfulPayment(currency='XTR', total_amount=100, invoice_payload='order:' + order['id'],
                                telegram_payment_charge_id='paid-charge', provider_payment_charge_id='')
    await dp.feed_update(bot, Update(update_id=2, message=message(payment=payment)))
    assert store.order(order['id'])['status'] == 'paid'


async def test_ticket_conversation_survives_dispatcher_restart(store, panel):
    session = TelegramSession()
    bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session)
    dp = build_dispatcher(store, Provisioner(store, panel), bot)
    query = CallbackQuery(id='ticket', from_user=actor(), chat_instance='x', data='ticket:new', message=message())
    await dp.feed_update(bot, Update(update_id=1, callback_query=query))
    assert store.user(1)['state'] == 'ticket'
    restarted = build_dispatcher(store, Provisioner(store, panel), bot)
    await restarted.feed_update(bot, Update(update_id=2, message=message('لطفاً راهنمایی کنید')))
    row = store.db.one("SELECT * FROM ticket_messages")
    assert row['body'] == 'لطفاً راهنمایی کنید'
    assert store.user(1)['state'] == ''


async def test_cross_user_service_callbacks_do_not_disclose_subscription(store, panel):
    p = Provisioner(store, panel)
    order = store.create_order(1, 1)
    store.pay_wallet(order['id'], 1)
    await p.process_one()
    store.register(2, 'Bob')
    session = TelegramSession()
    bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', session=session)
    dp = build_dispatcher(store, p, bot)
    query = CallbackQuery(id='stolen', from_user=actor(2), chat_instance='x', data='svc:'+order['id'], message=message(user_id=2))
    await dp.feed_update(bot, Update(update_id=1, callback_query=query))
    sent = ' '.join(getattr(c, 'text', '') or '' for c in session.calls)
    assert json.loads(order['snapshot'])['sub_id'] not in sent
    assert 'سرویس پیدا نشد' in sent
