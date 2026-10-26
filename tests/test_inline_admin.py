from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import SendAnimation, SendMessage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, ReplyKeyboardRemove, Update

from farstar.bot import build_dispatcher
from farstar.services import Provisioner
from test_bot import TelegramSession, actor, message


def setup(store,panel):
    session=TelegramSession()
    bot=Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi',session=session,
            default=DefaultBotProperties(parse_mode='HTML'))
    return session,bot,build_dispatcher(store,Provisioner(store,panel),bot)


async def click(dp,bot,data,user_id=1,index=1):
    query=CallbackQuery(id=str(index),from_user=actor(user_id),chat_instance='x',data=data,message=message(user_id=user_id))
    return await dp.feed_update(bot,Update(update_id=index,callback_query=query))


async def test_new_menu_has_only_inline_buttons_and_role_specific_admin_entry(store,panel):
    store.settings.mini_app_url='https://shop.example/app'
    store.settings.admin_mini_app_url='https://shop.example/admin-app'
    session,bot,dp=setup(store,panel)
    await dp.feed_update(bot,Update(update_id=1,message=message('/start')))
    sent=[c for c in session.calls if isinstance(c,SendMessage)]
    assert any(isinstance(c.reply_markup,ReplyKeyboardRemove) for c in sent)
    menu=next(c.reply_markup for c in sent if isinstance(c.reply_markup,InlineKeyboardMarkup) and any(b.web_app for row in c.reply_markup.inline_keyboard for b in row))
    assert not any(b.callback_data=='adm:home' for row in menu.inline_keyboard for b in row)
    assert all(c.reply_markup is None or isinstance(c.reply_markup,(ReplyKeyboardRemove,InlineKeyboardMarkup)) for c in sent)
    session.calls.clear()
    await dp.feed_update(bot,Update(update_id=2,message=message('/start',user_id=999)))
    assert any(b.callback_data=='adm:home' for c in session.calls if isinstance(c,SendMessage) and isinstance(c.reply_markup,InlineKeyboardMarkup)
               for row in c.reply_markup.inline_keyboard for b in row)


async def test_customer_cannot_invoke_admin_callbacks(store,panel):
    session,bot,dp=setup(store,panel)
    await click(dp,bot,'adm:credit:1')
    assert not store.user(1)['state'].startswith('admin:')
    assert store.user(1)['balance']==1000
    assert any('فقط برای مدیر' in (getattr(c,'text','') or '') for c in session.calls)


async def test_admin_can_create_plan_entirely_in_bot(store,panel):
    _,bot,dp=setup(store,panel)
    await click(dp,bot,'adm:planserver:1',999)
    for i,text in enumerate(['پلن تست','20','30','400000','2'],2):
        await dp.feed_update(bot,Update(update_id=i,message=message(text,user_id=999)))
    assert store.user(999)['state']=='admin:newplan-confirm'
    await click(dp,bot,'adm:saveplan',999,10)
    plan=store.db.one('SELECT * FROM plans ORDER BY id DESC LIMIT 1')
    assert plan['name']=='پلن تست' and plan['price']==400000 and plan['volume_gb']==20
    assert store.user(999)['state']==''


async def test_admin_wallet_change_needs_confirmation_and_deduplicates(store,panel):
    _,bot,dp=setup(store,panel)
    await click(dp,bot,'adm:credit:1',999)
    await dp.feed_update(bot,Update(update_id=2,message=message('۲۰۰۰۰۰',user_id=999)))
    assert store.user(1)['balance']==1000
    await click(dp,bot,'adm:creditconfirm',999,3)
    await click(dp,bot,'adm:creditconfirm',999,4)
    assert store.user(1)['balance']==201000


async def test_selecting_device_sends_animation_in_chat(store,panel):
    session,bot,dp=setup(store,panel)
    await click(dp,bot,'guide:android')
    animations=[c for c in session.calls if isinstance(c,SendAnimation)]
    assert len(animations)==1
    assert 'Happ' in animations[0].caption
    assert any('play.google.com' in (b.url or '') for row in animations[0].reply_markup.inline_keyboard for b in row)
