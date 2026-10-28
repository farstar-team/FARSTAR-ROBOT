import json
import asyncio
import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile, CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup,
    ErrorEvent, LabeledPrice, LinkPreviewOptions, Message, PreCheckoutQuery, ReplyKeyboardRemove, WebAppInfo,
)

from .panel import PanelError
from .admin_bot import AdminBot
from .happ import device_keyboard, send_guide
from .security import rate_limit
from .services import GIB, subscription
from .qr import subscription_card
from .store import DomainError, now
from .experience import order_progress, service_health
from .branding import branding

TERMS = (
    "📃 <b>قوانین خرید</b>\n\n"
    "حجم و مدت هر سرویس پیش از پرداخت نمایش داده می‌شود. اعتبار از زمان فعال‌سازی شروع می‌شود. "
    "در تمدید، حجم خریداری‌شده به سهمیه قبلی و زمان به انتهای اعتبار باقی‌مانده اضافه می‌شود. "
    "پس از پایان زمان یا حجم، سرویس متوقف می‌شود.\n\n"
    "لینک اشتراک محرمانه است؛ آن را در اختیار دیگران نگذارید. "
    "تست رایگان برای هر حساب یک بار قابل دریافت است. کیفیت اتصال به وضعیت شبکه نیز وابسته است.\n\n"
    "برای مشکل پرداخت، تحویل یا درخواست بازپرداخت از /paysupport استفاده کنید. "
    "مسئول رسیدگی، پشتیبانی همین فروشگاه است."
)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data, style='success' if data.startswith('card:') else None)
         for text, data in row] for row in rows
    ])


def build_dispatcher(store, provisioner, bot):
    router = Router()
    dp = Dispatcher()
    db, settings = store.db, store.settings
    brand = branding(store)

    def terms():
        return escape(brand.text('terms')) if brand.text('terms') else TERMS

    def money(amount):
        return f"{amount:,} {'⭐ استار' if settings.currency == 'XTR' else 'تومان'}"

    def menu(user_id):
        rows = [[(brand.text('buy_button'), 'menu:buy'), (brand.text('services_button'), 'menu:services')],
                [(brand.text('wallet_button'), 'menu:wallet'), (brand.text('help_button'), 'menu:help')],
                [('🎁 دعوت دوستان', 'menu:referrals'), (brand.text('orders_button'), 'menu:orders')],
                [(brand.text('support_button'), 'menu:support'), ('📃 قوانین', 'menu:terms')]]
        rows.append([('🔔 تنظیم هشدارهای اشتراک', 'preferences')])
        if settings.trial_enabled:
            rows.insert(2, [('🧪 دریافت تست رایگان', 'menu:trial')])
        result = keyboard(rows)
        if settings.mini_app_url:
            result.inline_keyboard.insert(0, [InlineKeyboardButton(text='✦ فروشگاه من · مینی‌اپ',
                                                web_app=WebAppInfo(url=settings.mini_app_url))])
        if user_id in settings.admin_ids:
            result.inline_keyboard.append([InlineKeyboardButton(text='⚙️ مدیریت ربات', callback_data='adm:home')])
            if settings.admin_mini_app_url:
                result.inline_keyboard.append([InlineKeyboardButton(text='🧩 مینی‌اپ مدیر',
                                                web_app=WebAppInfo(url=settings.admin_mini_app_url))])
        if getattr(store, 'license_authority', None):
            result.inline_keyboard.append([InlineKeyboardButton(text='🚀 ربات خودت را بساز · لایسنس رایگان', callback_data='license:home')])
        return result

    async def send(chat_id, text, reply_markup=None):
        if reply_markup is None:
            reply_markup = keyboard([[('⌂ منوی اصلی', 'menu:home')]])
        return await bot.send_message(chat_id, text, reply_markup=reply_markup,
                                      link_preview_options=LinkPreviewOptions(is_disabled=True))

    class Guard(BaseMiddleware):
        async def __call__(self, handler, event, data):
            actor = event.from_user
            if not actor:
                return
            is_payment = isinstance(event, PreCheckoutQuery) or (
                isinstance(event, Message) and event.successful_payment is not None)
            if isinstance(event, Message) and event.chat.type != "private":
                return
            if isinstance(event, CallbackQuery) and (not event.message or event.message.chat.type != "private"):
                return
            referral = None
            if isinstance(event, Message) and event.text and event.text.startswith("/start ref_"):
                raw = event.text.split("ref_", 1)[1]
                if raw.isdigit():
                    referral = int(raw)
            user = store.register(actor.id, actor.full_name, actor.username or "", referral)
            if is_payment:
                return await handler(event, data)
            support_action = (isinstance(event, Message) and (
                (event.text or "").split(" ")[0] in {"/support", "/paysupport", "/terms"}
                or event.text == "💬 پشتیبانی" or user["state"] == "ticket")) or (
                isinstance(event, CallbackQuery) and (event.data or "").startswith("ticket:"))
            support_action = support_action or (isinstance(event, CallbackQuery) and event.data in {'menu:support', 'menu:terms'})
            if user["banned"] and not support_action:
                if isinstance(event, CallbackQuery):
                    await event.answer("حساب شما مسدود است.", show_alert=True)
                else:
                    await send(actor.id, "حساب شما مسدود است؛ برای پیگیری /support را بزنید.")
                return
            if not rate_limit(db, f"bot:{actor.id}", 120 if actor.id in settings.admin_ids else 35, 60):
                if isinstance(event, CallbackQuery):
                    await event.answer("کمی صبر کنید و دوباره امتحان کنید.")
                return
            if settings.required_channel and actor.id not in settings.admin_ids and not support_action:
                try:
                    member = await bot.get_chat_member(settings.required_channel, actor.id)
                    joined = member.status in {"creator", "administrator", "member"} or (
                        member.status == "restricted" and getattr(member, "is_member", False))
                except TelegramAPIError:
                    joined = False
                if not joined:
                    buttons = [[InlineKeyboardButton(text="عضویت در کانال", url=settings.channel_url)]] if settings.channel_url else []
                    buttons.append([InlineKeyboardButton(text="عضو شدم ✓", callback_data="joined")])
                    await send(actor.id, "برای استفاده از فروشگاه ابتدا عضو کانال شوید.", InlineKeyboardMarkup(inline_keyboard=buttons))
                    if isinstance(event, CallbackQuery):
                        await event.answer()
                    return
            return await handler(event, data)

    guard = Guard()
    router.message.outer_middleware(guard)
    router.callback_query.outer_middleware(guard)
    router.pre_checkout_query.outer_middleware(guard)

    async def home(user_id):
        store.set_state(user_id)
        migrated = db.one('SELECT 1 FROM meta WHERE key=?', (f'inline-menu:{user_id}',))
        if not migrated:
            await send(user_id, '✦ منوی جدید Farstar آماده است.', ReplyKeyboardRemove())
            with db.transaction() as con:
                con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (f'inline-menu:{user_id}', '1'))
        await send(user_id, f"✦ <b>{escape(settings.app_name)}</b>\n\n{escape(brand.text('welcome'))}", menu(user_id))
        if not store.user(user_id)["terms_at"]:
            await send(user_id, terms(), keyboard([[('قوانین را خواندم و می‌پذیرم ✓', 'terms:accept')]]))

    async def show_plans(user_id, service_id=None):
        plans = store.plans()
        if service_id:
            service = db.one("SELECT * FROM services WHERE id=? AND user_id=?", (service_id, user_id))
            if not service or service["is_trial"]:
                raise DomainError("سرویس قابل تمدید پیدا نشد.")
            plans = [p for p in plans if p["server_id"] == service["server_id"]]
        if not plans:
            await send(user_id, "هنوز پلن قابل فروش موجود نیست؛ کمی بعد سر بزنید.")
            return
        rows = [[(f"{p['name']} · {p['volume_gb']}GB · {p['days']} روز · {money(p['price'])}",
                  f"plan:{p['id']}:{service_id or '-'}")] for p in plans[:40]]
        await send(user_id, "🛍 <b>پلن‌های فروشگاه</b>\n\nیک پلن برای مشاهده جزئیات انتخاب کن.", keyboard(rows))

    async def show_order(order):
        snap = json.loads(order["snapshot"])
        body = (f"🧾 <b>فاکتور خرید</b>\n\n{escape(snap['name'])}\n"
                f"حجم: {snap['volume_gb']} گیگابایت\nاعتبار: {snap['days']:g} روز\n"
                f"مبلغ: <b>{money(order['price'])}</b>\nشماره: <code>{order['id']}</code>\n\n"
                "با پرداخت، قوانین فروشگاه را تأیید می‌کنید. مهلت فاکتور ۳۰ دقیقه است.")
        progress = order_progress(db, order)
        body += '\n\n' + '\n'.join(('✅ ' if step['done'] else '▫️ ') + step['label'] for step in progress['steps'])
        if order["status"] in {'paid','processing','completed'}:
            status='✅ سفارش تحویل شد؛ لینک و QR را از سرویس‌های من ببین.' if order['status']=='completed' else '✅ سفارش در صف فعال‌سازی است.'
            await send(order["user_id"], body + '\n\n' + status)
            return
        if order['status']=='awaiting_review':
            await send(order['user_id'],body+'\n\n🧾 رسید دریافت شد؛ پس از تأیید مدیر، اشتراک و QR مستقیم ارسال می‌شود.',
                       keyboard([[('🔄 پیگیری وضعیت سفارش', 'order:'+order['id'])], [('💬 پشتیبانی','menu:support')]]))
            return
        if order['status']!='pending' or order['expires_at']<now():
            await send(order['user_id'],body+'\n\nاین فاکتور قابل پرداخت نیست؛ از وضعیت سفارش یا پشتیبانی پیگیری کن.')
            return
        rows = []
        if settings.manual_payments and settings.currency=='IRT':
            rows.append([('📸 پرداخت و ارسال فیش بانکی','card:'+order['id'])])
        if store.user(order['user_id'])['balance'] >= order['price']:
            rows.append([("پرداخت با موجودی قبلی کیف پول", "wallet:" + order["id"])])
        if settings.currency == "XTR":
            rows.insert(0, [("پرداخت با استار ⭐", "stars:" + order["id"])])
        rows.append([("لغو فاکتور", "cancel-order:" + order["id"])])
        await send(order["user_id"], body, keyboard(rows))

    async def show_services(user_id, offset=0):
        rows = db.all("""SELECT v.*,s.name server_name,COALESCE(l.label,'') label FROM services v JOIN servers s ON s.id=v.server_id
            LEFT JOIN service_labels l ON l.service_id=v.id
            WHERE v.user_id=? ORDER BY v.created_at DESC LIMIT 16 OFFSET ?""", (user_id, offset))
        if not rows:
            await send(user_id, "هنوز سرویسی نداری. از «خرید سرویس» یا «تست رایگان» شروع کن.")
            return
        keys = [[(f"{'🟢' if v['enabled'] and v['expires_at'] > now() and v['used_bytes'] < v['total_bytes'] else '⚪'} {v['label'] or v['server_name']} · {v['email']}",
                  "svc:" + v["id"])] for v in rows[:15]]
        if len(rows) > 15:
            keys.append([("صفحه بعد ←", f"services:{offset + 15}")])
        await send(user_id, "📡 <b>سرویس‌های من</b>\nبرای دریافت لینک و مدیریت، سرویس را انتخاب کن.", keyboard(keys))

    def owned_service(service_id, user_id):
        return store.owned_service(service_id, user_id)

    async def show_service(service_id, user_id, sync=False):
        service = owned_service(service_id, user_id)
        store.set_state(user_id)
        warning = ""
        if sync:
            try:
                await provisioner.sync_service(service)
                service = owned_service(service_id, user_id)
            except (PanelError, DomainError):
                warning = "\n⚠️ اتصال به پنل برقرار نشد؛ آخرین اطلاعات ذخیره‌شده نمایش داده می‌شود."
        server = store.server(service["server_id"])
        days = max(0, (service["expires_at"] - now()) // 86400)
        used = service["used_bytes"] / GIB
        total = service["total_bytes"] / GIB
        live = service["enabled"] and service["expires_at"] > now() and service["used_bytes"] < service["total_bytes"]
        date = datetime.fromtimestamp(service["expires_at"], ZoneInfo("Asia/Tehran")).strftime("%Y/%m/%d %H:%M")
        synced = datetime.fromtimestamp(service["synced_at"] or service["created_at"], ZoneInfo("Asia/Tehran")).strftime("%m/%d %H:%M")
        link = subscription(server, service["sub_id"])
        rows = [[("🔄 بروزرسانی مصرف", "refresh:" + service_id), ("📱 QR اتصال", "qr:" + service_id)],
                [("🔗 کانفیگ‌ها", "links:" + service_id)],
                [('🩺 بررسی مشکل اتصال', 'diagnose:'+service_id), ('✏️ نام دلخواه', 'rename:'+service_id)]]
        if not service["is_trial"]:
            rows.append([("♻️ تمدید و افزایش حجم", "renew:" + service_id)])
        markup = keyboard(rows)
        if len(link) <= 256:
            markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='📋 کپی لینک اشتراک', copy_text=CopyTextButton(text=link),style='primary')])
        await send(user_id, f"📡 <b>{escape(service['label'] or server['name'])}</b> · <code>{escape(service['email'])}</code>\n\n"
                   f"وضعیت: {'فعال 🟢' if live else 'پایان اعتبار / غیرفعال ⚪'}\n"
                   f"مصرف: {used:.2f} از {total:g} گیگابایت\nباقی‌مانده زمان: {days} روز\n"
                   f"حجم باقی‌مانده: {max(0,total-used):.2f} گیگابایت\n"
                   f"پایان اعتبار: {date}\nآخرین همگام‌سازی: {synced}\n\n<code>{escape(link)}</code>{warning}", markup)

    async def preferences(user_id):
        prefs = store.notification_preferences(user_id)
        await send(user_id, '🔔 <b>هشدارهای اشتراک</b>\n\nهشدار زمان در ۷۲ و ۲۴ ساعت پایانی، هشدار حجم در مصرف ۸۰٪ و ۹۵٪ و پایان اعتبار ارسال می‌شود. '
                   'هر مرحله فقط یک بار است؛ پیام‌های خرید و پشتیبانی همیشه ارسال می‌شوند.', keyboard([
                       [(('✅' if prefs['expiry_alerts'] else '⚪')+' هشدار پایان زمان', 'preferences:expiry_alerts')],
                       [(('✅' if prefs['quota_alerts'] else '⚪')+' هشدار پایان حجم', 'preferences:quota_alerts')],
                       [('⌂ منوی اصلی','menu:home')]]))

    async def show_wallet(user_id):
        user = store.user(user_id)
        entries = db.all("SELECT amount,kind FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 5", (user_id,))
        history = "\n".join(f"{'+' if e['amount'] > 0 else ''}{money(e['amount'])}" for e in entries)
        keys = [[("🎟 ثبت کد هدیه", "gift")]]
        keys.insert(0, [('🛍 خرید مستقیم با کارت‌به‌کارت','menu:buy')])
        await send(user_id, f"💰 <b>کیف پول</b>\n\nموجودی: {money(user['balance'])}\n\n{history}", keyboard(keys))

    async def support(user_id):
        tickets = db.all("SELECT * FROM tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 10", (user_id,))
        keys = [[("✍️ تیکت تازه", "ticket:new")]] + [[(f"#{t['id']} · {t['subject'][:40]}", f"ticket:{t['id']}")] for t in tickets]
        await send(user_id, '💬 <b>پشتیبانی</b>\n' + escape(brand.text('support')), keyboard(keys))

    async def referrals(user_id):
        me = await bot.get_me()
        count = db.one('SELECT COUNT(*) n FROM users WHERE referrer_id=?', (user_id,))['n']
        await send(user_id, f"🎁 <b>دعوت دوستان</b>\n{settings.referral_percent}٪ اولین خرید هر دوست به کیف پولت برمی‌گردد.\n"
                   f"دعوت‌شده‌ها: {count}\n\nhttps://t.me/{me.username}?start=ref_{user_id}")

    async def orders(user_id):
        rows = db.all('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (user_id,))
        labels = {'pending':'منتظر پرداخت','awaiting_review':'منتظر تأیید رسید','checkout':'در حال پرداخت','paid':'در صف ساخت','processing':'در حال ساخت',
                  'completed':'تحویل شد','failed':'بررسی پشتیبانی','expired':'منقضی','refunded':'بازپرداخت شد',
                  'refund_pending':'در صف بازپرداخت','cancelled':'لغو شد'}
        await send(user_id, '🧾 <b>سفارش‌های من</b>\n\n'+ ('\n'.join(
            f"<code>{o['id']}</code> · {money(o['price'])} · {labels.get(o['status'],o['status'])}" for o in rows) or 'هنوز سفارشی نداری.'),
            keyboard([[(f"{o['id']} · {labels.get(o['status'],o['status'])}",'order:'+o['id'])] for o in rows]))

    admin = AdminBot(store, provisioner, bot, send)

    @router.pre_checkout_query()
    async def precheckout(query: PreCheckoutQuery):
        try:
            if not query.invoice_payload.startswith("order:"):
                raise DomainError("فاکتور نامعتبر است.")
            store.precheckout(query.invoice_payload[6:], query.from_user.id, query.total_amount, query.currency)
            await query.answer(ok=True)
        except DomainError as exc:
            await query.answer(ok=False, error_message=str(exc))

    @router.message(F.successful_payment)
    async def payment(message: Message):
        p = message.successful_payment
        try:
            if not p.invoice_payload.startswith("order:"):
                raise DomainError("فاکتور نامعتبر است؛ با /paysupport تماس بگیرید.")
            store.settle_stars(p.invoice_payload[6:], message.from_user.id, p.total_amount, p.currency,
                               p.telegram_payment_charge_id)
            await send(message.chat.id, "✅ پرداخت ثبت شد. نتیجه فعال‌سازی همین‌جا ارسال می‌شود.")
        except DomainError as exc:
            await send(message.chat.id, str(exc))

    @router.callback_query()
    async def callback(query: CallbackQuery):
        user_id = query.from_user.id
        data = query.data or ""
        try:
            await query.answer()
            parts = data.split(":")
            action = parts[0]
            if action in {'license', 'licadm'}:
                issuer = getattr(store, 'license_authority', None)
                if not issuer:
                    raise DomainError('مرجع دریافت لایسنس: @FARSTAR_VPN_BOT')
                await issuer.callback(user_id, data, send)
            elif action == 'adm':
                await admin.callback(query)
            elif action == 'guide':
                await send_guide(bot, store, user_id, parts[1])
            elif action == 'menu':
                store.set_state(user_id)
                selected = parts[1]
                if selected == 'home':
                    await home(user_id)
                elif selected == 'buy':
                    await show_plans(user_id)
                elif selected == 'services':
                    await show_services(user_id)
                elif selected == 'wallet':
                    await show_wallet(user_id)
                elif selected == 'help':
                    await send(user_id, '📱 <b>راهنمای Happ</b>\n' + escape(brand.text('help')), device_keyboard())
                elif selected == 'support':
                    await support(user_id)
                elif selected == 'terms':
                    await send(user_id, terms(), keyboard([[('قوانین را می‌پذیرم ✓', 'terms:accept')]]))
                elif selected == 'trial':
                    await show_order(store.create_order(user_id, trial=True))
                elif selected == 'referrals':
                    await referrals(user_id)
                elif selected == 'orders':
                    await orders(user_id)
                else:
                    raise DomainError('دکمه معتبر نیست.')
            elif action == "terms":
                store.accept_terms(user_id)
                await home(user_id)
            elif action == "joined":
                await home(user_id)
            elif action == "plan":
                plan_id, renew = int(parts[1]), parts[2]
                plan = next((p for p in store.plans() if p["id"] == plan_id), None)
                if not plan:
                    raise DomainError("پلن در دسترس نیست.")
                await send(user_id, f"✦ <b>{escape(plan['name'])}</b>\n\n"
                           f"🌍 {escape(plan['server_name'])} · {escape(plan['location'])}\n"
                           f"📦 {plan['volume_gb']}GB · {plan['days']} روز\n"
                           f"تعداد IP هم‌زمان: {plan['ip_limit'] or 'نامحدود'}\n"
                           f"{escape(plan['description'])}\n\n<b>{money(plan['price'])}</b>",
                           keyboard([[("تأیید و ساخت فاکتور", f"buy:{plan_id}:{renew}")],
                                     [("کد تخفیف دارم 🎟", f"coupon:{plan_id}:{renew}")]]))
            elif action == "buy":
                await show_order(store.create_order(user_id, int(parts[1]), service_id=None if parts[2] == "-" else parts[2]))
            elif action == "coupon":
                store.set_state(user_id, "coupon", {"plan_id": int(parts[1]), "service_id": None if parts[2] == "-" else parts[2]})
                await send(user_id, "کد تخفیف را بفرست. برای لغو /cancel را بزن.")
            elif action == "wallet":
                store.pay_wallet(parts[1], user_id)
                await send(user_id, "✅ پرداخت از کیف پول ثبت شد. سرویس به‌زودی تحویل داده می‌شود.")
            elif action == 'order':
                await show_order(store.order(parts[1],user_id))
            elif action == 'card':
                if not settings.manual_payments or not settings.card_number or not settings.card_holder:
                    raise DomainError('پرداخت کارت‌به‌کارت هنوز آماده نیست.')
                order=store.receipt_order(parts[1],user_id)
                store.set_state(user_id,'receipt_photo',{'order_id':order['id'],'amount':order['price']})
                markup = keyboard([[('↩ سفارش من','order:'+order['id'])]])
                markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='📋 کپی شماره کارت',copy_text=CopyTextButton(text=settings.card_number))])
                if settings.mini_app_url:
                    markup.inline_keyboard.insert(0, [InlineKeyboardButton(text='📸 انتخاب عکس و ارسال فیش بانکی',
                        web_app=WebAppInfo(url=settings.mini_app_url+'#receipt-'+order['id']),style='success')])
                await send(user_id,f"📸 <b>پرداخت و ارسال فیش بانکی</b>\n\n۱. مبلغ دقیق <b>{money(order['price'])}</b> را واریز کن:\n"
                           f"<code>{escape(settings.card_number)}</code>\nبه نام {escape(settings.card_holder)}\n"
                           f"سفارش: <code>{order['id']}</code>\n\n۲. دکمه سبز زیر را بزن و عکس فیش را انتخاب کن. "
                           'همچنین می‌توانی از گیره 📎 تلگرام، عکس را همین‌جا بفرستی.\n\n'
                           '۳. بعد از تأیید مدیر، اشتراک با لینک و QR مستقیم ارسال می‌شود.\n\n' + escape(brand.text('payment_note')), markup)
            elif action == 'preferences':
                if len(parts) > 1:
                    pref = parts[1]
                    current = store.notification_preferences(user_id)
                    if pref not in current:
                        raise DomainError('تنظیم معتبر نیست.')
                    store.save_notification_preferences(user_id, {pref: not current[pref]})
                await preferences(user_id)
            elif action == 'rename':
                owned_service(parts[1], user_id)
                store.set_state(user_id, 'service_label', {'service_id': parts[1]})
                await send(user_id, '✏️ یک نام دلخواه مثل «گوشی من» یا «مغازه» بفرست (تا ۴۰ نویسه).\n'
                           'نام اصلی FARSTAR و لینک اتصال حفظ می‌شود. برای حذف نام، یک خط تیره - بفرست.',
                           keyboard([[('↩ سرویس من','svc:'+parts[1])]]))
            elif action == 'diagnose':
                service = owned_service(parts[1], user_id)
                if not rate_limit(db, f'sync:{user_id}', 6, 60):
                    raise DomainError('برای بررسی دوباره کمی صبر کن.')
                unavailable = False
                try:
                    if not settings.demo:
                        await provisioner.sync_service(service)
                except (DomainError, PanelError):
                    unavailable = True
                result = service_health(owned_service(parts[1], user_id))
                checks = '\n'.join(('✅ ' if c['ok'] else '❌ ') + c['label'] for c in result['checks'])
                warning = '\n⚠️ پنل در دسترس نبود؛ این نتیجه بر اساس آخرین اطلاعات ذخیره‌شده است.' if unavailable or not result['fresh'] else ''
                rows = [[('📱 راهنمای Happ','menu:help'), ('💬 گزارش مشکل','service-support:'+parts[1])],
                        [('↩ سرویس من','svc:'+parts[1])]]
                if not service['is_trial']:
                    rows.insert(0, [('♻️ تمدید همین اشتراک','renew:'+parts[1])])
                await send(user_id, f"🩺 <b>{escape(result['title'])}</b>\n\n{checks}\n\n{escape(result['advice'])}{warning}\n\n"
                           + escape(result['note']), keyboard(rows))
            elif action == 'service-support':
                owned_service(parts[1], user_id)
                store.set_state(user_id, 'ticket', {'service_id': parts[1]})
                await send(user_id, '💬 مدل دستگاه، اینترنت مورد استفاده و خطای Happ را بنویس. مشخصات حساب خودکار به گزارش اضافه می‌شود.')
            elif action == "cancel-order":
                store.cancel_order(parts[1], user_id)
                await send(user_id, "فاکتور لغو شد و ظرفیت آن آزاد شد.")
            elif action == "stars":
                order = store.order(parts[1], user_id)
                if order["status"] != "pending" or order["expires_at"] < now() or order["currency"] != "XTR":
                    raise DomainError("فاکتور قابل پرداخت نیست؛ سفارش تازه بسازید.")
                await bot.send_invoice(chat_id=user_id, title="خرید سرویس " + settings.app_name[:20],
                                       description=json.loads(order["snapshot"])["name"][:255],
                                       payload="order:" + order["id"], currency="XTR",
                                       prices=[LabeledPrice(label="سرویس", amount=order["price"])],
                                       provider_token="", start_parameter="order")
            elif action == "services":
                await show_services(user_id, max(0, int(parts[1])))
            elif action in {"svc", "refresh"}:
                if action == "refresh" and not rate_limit(db, f"sync:{user_id}", 6, 60):
                    raise DomainError("برای بروزرسانی مصرف کمی صبر کنید.")
                await show_service(parts[1], user_id, sync=action == "refresh")
            elif action == "qr":
                service = owned_service(parts[1], user_id)
                link = subscription(store.server(service["server_id"]), service["sub_id"])
                card=await asyncio.to_thread(subscription_card,link,service)
                await bot.send_photo(user_id, BufferedInputFile(card, filename=service['email']+'.png'),
                                     caption=f"📱 QR اختصاصی <code>{escape(service['email'])}</code>\n<code>{escape(link)}</code>")
            elif action == "links":
                service = owned_service(parts[1], user_id)
                async with provisioner.panel(store.server(service["server_id"])) as panel:
                    links = await panel.links(service["email"])
                if not links:
                    await send(user_id, "برای این پروتکل از لینک اشتراک و اپ سازگار استفاده کنید.")
                for link in (links or [])[:15]:
                    if isinstance(link, str):
                        await send(user_id, "<code>" + escape(link[:3800]) + "</code>")
            elif action == "renew":
                await show_plans(user_id, parts[1])
            elif action == "gift":
                store.set_state(user_id, "gift")
                await send(user_id, "کد هدیه را بفرست. برای لغو /cancel را بزن.")
            elif action == "topup":
                store.set_state(user_id)
                await show_plans(user_id)
            elif action == "ticket":
                if parts[1] == "new":
                    store.set_state(user_id, "ticket")
                    await send(user_id, "موضوع و متن مشکلت را در یک پیام بفرست. برای لغو /cancel را بزن.")
                else:
                    ticket_id = int(parts[1])
                    ticket = db.one("SELECT * FROM tickets WHERE id=? AND user_id=?", (ticket_id, user_id))
                    if not ticket:
                        raise DomainError("تیکت پیدا نشد.")
                    messages = db.all("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 8", (ticket_id,))
                    for item in reversed(messages):
                        await send(user_id, f"{'پشتیبانی' if item['sender'] == 'admin' else 'شما'}:\n{escape(item['body'])}")
                    store.set_state(user_id, "ticket", {"ticket_id": ticket_id})
                    await send(user_id, "برای افزودن پاسخ، متن را بفرست؛ برای خروج /cancel.")
            elif action == "receipt" and user_id in settings.admin_ids:
                store.review_receipt(parts[2], parts[1] == "approve", user_id)
                await send(user_id, "رسید بررسی شد.")
            else:
                raise DomainError("این دکمه معتبر نیست؛ /start را بزنید.")
        except (DomainError, PanelError) as exc:
            await send(user_id, escape(str(exc)))
        except (ValueError, IndexError):
            await send(user_id, "درخواست معتبر نیست؛ /start را بزنید.")

    @router.message()
    async def message_handler(message: Message):
        user_id = message.from_user.id
        text = message.text or ""
        command = text.split()[0].split("@")[0] if text else ""
        try:
            if command in {"/start", "/cancel"}:
                await home(user_id)
                return
            if command == "/terms":
                await send(user_id, terms(), keyboard([[('قوانین را می‌پذیرم ✓', 'terms:accept')]]))
                return
            if command in {"/support", "/paysupport"} or text == "💬 پشتیبانی":
                store.set_state(user_id)
                await support(user_id)
                return
            if user_id in settings.admin_ids:
                if command == "/admin":
                    await admin.home(user_id)
                    return
                if command == "/reply":
                    _, ticket_id, body = text.split(maxsplit=2)
                    store.ticket(user_id, body[:3000], int(ticket_id), admin=True)
                    await send(user_id, "پاسخ برای کاربر در صف ارسال قرار گرفت.")
                    return
                if await admin.message(message):
                    return
                if command == "/receipt":
                    receipt_id = text.split(maxsplit=1)[1]
                    await admin.receipt(user_id,receipt_id)
                    return
            # Main menu always cancels a pending conversation state.
            if text in {"🛍 خرید سرویس", "📡 سرویس‌های من", "💰 کیف پول", "🧪 تست رایگان", "🎁 دعوت دوستان", "🧾 سفارش‌های من", "📚 راهنمای اتصال"}:
                store.set_state(user_id)
            if text == "🛍 خرید سرویس":
                await show_plans(user_id)
            elif text == "📡 سرویس‌های من" or command == "/services":
                await show_services(user_id)
            elif text == "💰 کیف پول":
                await show_wallet(user_id)
            elif text == "🧪 تست رایگان":
                await show_order(store.create_order(user_id, trial=True))
            elif text == "🎁 دعوت دوستان":
                await referrals(user_id)
            elif text == "🧾 سفارش‌های من":
                await orders(user_id)
            elif text == "📚 راهنمای اتصال" or command == '/help':
                await send(user_id, 'دستگاهت را برای دریافت آموزش Happ انتخاب کن.', device_keyboard())
            else:
                user = store.user(user_id)
                state_data = json.loads(user["state_data"])
                state = user["state"]
                if state == "coupon" and text:
                    order = store.create_order(user_id, state_data["plan_id"], text, state_data.get("service_id"))
                    store.set_state(user_id)
                    await show_order(order)
                elif state == "gift" and text:
                    amount = store.redeem(user_id, text)
                    store.set_state(user_id)
                    await send(user_id, f"🎉 {money(amount)} به کیف پول اضافه شد.")
                elif state == "ticket" and text:
                    if state_data.get('service_id'):
                        service = owned_service(state_data['service_id'], user_id)
                        text = f"سرویس: {service['email']}\n" + text
                    ticket_id = store.ticket(user_id, text[:3000], state_data.get("ticket_id"))
                    store.set_state(user_id)
                    await send(user_id, f"✅ تیکت #{ticket_id} ثبت شد؛ پاسخ در همین ربات می‌رسد.")
                elif state == 'service_label' and text:
                    store.rename_service(state_data['service_id'], user_id, '' if text.strip() == '-' else text)
                    store.set_state(user_id)
                    await show_service(state_data['service_id'], user_id)
                elif state == "topup_amount" and text:
                    store.set_state(user_id)
                    await send(user_id,'پرداخت کارت‌به‌کارت مستقیم برای سفارش انجام می‌شود؛ اول پلن را انتخاب کن.')
                    await show_plans(user_id)
                elif state == "receipt_photo" and message.photo:
                    if not state_data.get('order_id'):
                        store.set_state(user_id)
                        await send(user_id,'برای ثبت رسید، سفارش را از بخش سفارش‌های من انتخاب کن.')
                        await orders(user_id)
                        return
                    rid = store.add_receipt(user_id, state_data["amount"], message.photo[-1].file_id,state_data['order_id'],
                                            file_unique_id=message.photo[-1].file_unique_id)
                    store.set_state(user_id)
                    await send(user_id, f"🧾 رسید ثبت شد. کد پیگیری: <code>{rid}</code>\nپس از تأیید مدیر، اشتراک و QR همین‌جا ارسال می‌شود.")
                elif state == "receipt_photo":
                    await send(user_id, "لطفاً تصویر رسید را به صورت عکس بفرست؛ برای لغو /cancel.")
                else:
                    await send(user_id, "از دکمه‌های منو استفاده کن یا /start را بزن.", menu(user_id))
        except (DomainError, PanelError) as exc:
            await send(user_id, escape(str(exc)))
        except (ValueError, IndexError):
            await send(user_id, "فرمت پیام درست نیست؛ دوباره امتحان کن یا /cancel را بزن.")

    @router.errors()
    async def error_handler(event: ErrorEvent):
        logging.getLogger(__name__).warning("Telegram handler failed type=%s", type(event.exception).__name__)
        # Payment is durable before delivery; transient Telegram failures must not undo it.
        update = event.update
        actor = update.message.from_user if update.message else (
            update.callback_query.from_user if update.callback_query else None)
        if actor:
            try:
                await send(actor.id, "ارتباط موقتاً دچار مشکل شد. نتیجه سفارش را در «سفارش‌های من» ببینید یا /support را بزنید.")
            except TelegramAPIError:
                pass
        return True

    dp.include_router(router)
    return dp
